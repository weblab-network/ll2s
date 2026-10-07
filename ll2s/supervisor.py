"""Supervise the private OVS daemons and stop data ports when either exits."""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .backend import BackendError, OVSBridge, run
from .lldp import DiscoveryService
from .snmp import SnmpService


def main():
    runtime = Path(os.environ.get("LL2S_RUNTIME_DIR", "/run/ll2s"))
    runtime.mkdir(parents=True, exist_ok=True)
    service_lock = (runtime / "service.lock").open("a")
    try:
        fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("LL2S service is already running in this runtime directory", file=sys.stderr)
        return 1
    backend = OVSBridge(runtime)
    discovery = DiscoveryService(runtime)
    monitoring = SnmpService(runtime)
    (runtime / "ready").unlink(missing_ok=True)
    daemons = []
    child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)

    def wait_ready(check):
        for _ in range(100):
            if stopping or any(p.poll() is not None for p in daemons):
                raise BackendError("OVS daemon exited during startup")
            try:
                check()
                return
            except BackendError:
                time.sleep(0.1)
        raise BackendError("OVS daemons did not become ready")

    try:
        if not Path("/sys/module/openvswitch").exists():
            raise BackendError("host Open vSwitch module is not loaded; run 'sudo modprobe openvswitch' on the host")
        backend.stop_forwarding()  # Close any ports left by a prior instance.
        backend.ovsdir.mkdir(parents=True, exist_ok=True)
        database = backend.ovsdir / "conf.db"
        # Keep ownership metadata across crashes: the kernel datapath can survive
        # ovs-vswitchd. It must be adopted before removing only our old bridge.
        if not database.exists():
            run("ovsdb-tool", "create", database, "/usr/share/openvswitch/vswitch.ovsschema")
        daemons.append(subprocess.Popen([
            "ovsdb-server", str(database), f"--remote=punix:{backend.ovsdir}/db.sock",
            f"--unixctl={backend.ovsdir}/ovsdb-server.ctl", "--no-chdir",
            "--verbose=console:warn",
        ]))
        wait_ready(lambda: backend.vsctl("--no-wait", "init"))
        # Use explicit sockets so multiple isolated namespaces can run independently.
        daemons.append(subprocess.Popen([
            "ovs-vswitchd", f"unix:{backend.ovsdir}/db.sock",
            f"--unixctl={backend.ovsdir}/ovs-vswitchd.ctl", "--no-chdir",
            "--verbose=console:warn",
        ], env={**os.environ, "OVS_RUNDIR": str(backend.ovsdir)}))
        wait_ready(lambda: backend.appctl("version"))
        bridges, _, _ = backend.topology()
        old_bridge = bridges.get(backend.bridge)
        if old_bridge and old_bridge["external_ids"].get("ll2s-owner") == backend.owner:
            backend.vsctl("del-br", backend.bridge)
            # ovs-vsctl waits for reconfiguration; verify the old kernel device
            # also disappeared before treating startup as a fresh configuration.
            for _ in range(50):
                if backend.bridge not in backend.links():
                    break
                time.sleep(0.1)
            else:
                raise BackendError("old managed OVS bridge did not disappear during startup cleanup")
        for name in ("running.json", "FAILED", "active-ports.json"):
            (runtime / name).unlink(missing_ok=True)
        startup = Path(os.environ.get("LL2S_STARTUP", "/etc/ll2s/startup.conf"))
        if startup.exists():
            # Lab orchestrators may attach ports after the container is started.
            from .cli import Shell
            shell = Shell(backend, runtime=runtime, startup=startup)
            shell.load(startup.read_text())
            deadline = time.monotonic() + float(os.environ.get("LL2S_RESTORE_WAIT", "30"))
            while not set(shell.candidate.ports) <= set(backend.links()):
                if stopping or any(p.poll() is not None for p in daemons):
                    raise BackendError("service stopped while waiting for startup interfaces")
                if time.monotonic() >= deadline:
                    raise BackendError("startup interfaces did not arrive before LL2S_RESTORE_WAIT expired")
                time.sleep(0.1)
            # Serialize startup application with interactive configuration sessions.
            with (runtime / "lock").open("a") as config_lock:
                fcntl.flock(config_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                shell.commit()
        (runtime / "ready").write_text("ready\n")
        child = subprocess.Popen(sys.argv[1:] or ["sleep", "infinity"])
        while not stopping:
            if any(p.poll() is not None for p in daemons):
                raise BackendError("OVS daemon exited; stopping managed data ports")
            if child.poll() is not None:
                return child.returncode
            monitoring.tick()
            discovery.tick()
            time.sleep(0.1)
        return 0
    except (BackendError, OSError, ValueError) as e:
        print(f"LL2S startup/runtime error: {e}", file=sys.stderr)
        return 1
    finally:
        (runtime / "ready").unlink(missing_ok=True)
        try:
            backend.stop_forwarding()
        except (BackendError, OSError, ValueError) as e:
            print(f"LL2S port shutdown failed: {e}", file=sys.stderr)
        # Close data ports first: discovery cleanup must not delay fail-closed
        # behavior when a critical OVS daemon has failed.
        discovery.stop()
        monitoring.stop()
        for process in [child, *reversed(daemons)]:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        service_lock.close()


if __name__ == "__main__":
    sys.exit(main())
