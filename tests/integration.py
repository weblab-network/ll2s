"""Real RSTP triangle, run only inside the disposable test container."""

import json
import os
from pathlib import Path
import subprocess
import time

from ll2s.backend import decode


def run(*args, okay=True):
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=20)
    if okay and p.returncode:
        raise AssertionError(f"{' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p


def ns(name, *args, okay=True):
    return run("ip", "netns", "exec", name, *args, okay=okay)


def cli(name, *commands, okay=True):
    args = ["ll2sh", "--runtime-dir", f"/run/ll2s/{name}", "--startup", f"/tmp/{name}.conf"]
    for command in commands:
        args += ["-c", command]
    return ns(name, *args, okay=okay)


def wait_for(description, predicate, seconds=20):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if predicate():
            elapsed = time.monotonic() - started
            print(f"PASS: {description} ({elapsed:.3f}s)", flush=True)
            return elapsed
        time.sleep(0.1)
    raise AssertionError(f"timed out: {description}")


def ping(source="h1", target="192.0.2.2"):
    return ns(source, "ping", "-c", "1", "-W", "1", target, okay=False).returncode == 0


def rows(name, table, columns):
    result = json.loads(ns(name, "ovs-vsctl", f"--db=unix:/run/ll2s/{name}/ovs/db.sock",
                           "--format=json", f"--columns={columns}", "list", table).stdout)
    return [dict(zip(result["headings"], map(decode, row))) for row in result["data"]]


def port_status(name):
    return {p["name"]: p["rstp_status"] for p in rows(name, "Port", "name,rstp_status") if p["name"] != "br0"}


def link(a, an, b, bn):
    run("ip", "link", "add", "va", "type", "veth", "peer", "name", "vb")
    run("ip", "link", "set", "va", "netns", a)
    run("ip", "link", "set", "vb", "netns", b)
    ns(a, "ip", "link", "set", "va", "name", an)
    ns(b, "ip", "link", "set", "vb", "name", bn)


def start_service(name):
    ready = Path(f"/run/ll2s/{name}/ready")
    ready.unlink(missing_ok=True)
    log = open(f"/tmp/{name}.log", "a")
    process = subprocess.Popen([
        "ip", "netns", "exec", name, "env", f"LL2S_RUNTIME_DIR=/run/ll2s/{name}",
        f"LL2S_STARTUP=/tmp/{name}.conf", "ll2s-entrypoint", "sleep", "infinity"
    ], stdout=log, stderr=log)
    log.close()
    def check():
        if process.poll() is not None:
            raise AssertionError(Path(f"/tmp/{name}.log").read_text())
        return ready.exists()
    wait_for(f"{name} OVS service ready", check)
    return process


def main():
    if os.environ.get("LL2S_DISPOSABLE_TEST") != "1":
        raise SystemExit("Use scripts/test-integration; this test requires a disposable container.")
    switches = ("s1", "s2", "s3")
    for name in (*switches, "h1", "h2", "h3"):
        run("ip", "netns", "add", name)
        ns(name, "ip", "link", "set", "lo", "up")
    link("s1", "eth1", "s2", "eth1")
    link("s1", "eth2", "s3", "eth1")
    link("s2", "eth2", "s3", "eth2")
    link("s1", "eth3", "h1", "eth0")
    link("s2", "eth3", "h2", "eth0")
    link("s2", "eth4", "h3", "eth0")
    services = {}
    try:
        for name in switches:
            services[name] = start_service(name)
        for idx, host in enumerate(("h1", "h2", "h3"), 1):
            ns(host, "ip", "address", "add", f"192.0.2.{idx}/24", "dev", "eth0")
            ns(host, "ip", "link", "set", "eth0", "up")
        for idx, name in enumerate(switches, 1):
            commands = ["conf t", f"hostname {name}", "lldp run", "spanning-tree mode rstp", "vlan 10,20", f"spanning-tree priority {idx * 4096}"]
            for port in ("eth1", "eth2"):
                commands += [f"interface {port}", "switchport mode trunk",
                             "switchport trunk allowed vlan 10,20", "exit"]
            if name in ("s1", "s2"):
                commands += ["interface eth3", "switchport access vlan 10", "spanning-tree portfast", "exit"]
            if name == "s2":
                commands += ["interface eth4", "switchport access vlan 20", "spanning-tree portfast", "exit"]
            cli(name, *commands, "commit", "end", "write memory")
        wait_for("RSTP triangle forwards VLAN 10", ping, seconds=45)
        def blocked():
            return [(s, n) for s in switches for n, status in port_status(s).items()
                    if status.get("rstp_port_state") == "Discarding" and status.get("rstp_port_role") == "Alternate"]
        wait_for("exactly one alternate port discards", lambda: len(blocked()) == 1)
        print(f"Blocked port: {blocked()}", flush=True)
        bridges = [rows(s, "Bridge", "name,rstp_status")[0]["rstp_status"] for s in switches]
        root = bridges[0]["rstp_bridge_id"]
        assert all(b["rstp_root_id"] == root for b in bridges), bridges
        assert port_status("s2")["eth1"]["rstp_port_role"] == "Root"
        print(f"PASS: all switches elect s1 ({root})", flush=True)
        from lldp_checks import check_discovery
        check_discovery(switches, cli, ns, wait_for, ping, blocked)
        # Observe actual protocol version 2 BPDUs, not just a configuration flag.
        capture = '''import socket, time
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
s.bind(("eth1", 0)); s.settimeout(1)
end = time.monotonic() + 8
while time.monotonic() < end:
    try: p = s.recv(2048)
    except TimeoutError: continue
    if len(p) > 20 and p[:6] == bytes.fromhex("0180c2000000") and p[14:21] == bytes.fromhex("42420300000202"):
        print("RSTP BPDU version 2 observed"); break
else: raise SystemExit("No RSTP BPDU observed")
'''
        print("PASS: " + ns("s2", "python3", "-c", capture).stdout.strip(), flush=True)
        assert not ping(target="192.0.2.3"), "VLAN 10 leaked into VLAN 20"
        print("PASS: VLAN isolation (same subnet, different VLAN)", flush=True)
        mac = json.loads(ns("h1", "ip", "-j", "link", "show", "eth0").stdout)[0]["address"]
        assert mac in cli("s2", "show mac address-table").stdout
        print("PASS: source MAC learned across trunk", flush=True)
        before = cli("s1", "show running-config").stdout
        assert before == cli("s1", "show startup-config").stdout
        bad = cli("s1", "conf t", "interface eth3", "switchport access vlan 999", "exit", "commit", okay=False)
        assert bad.returncode and "declare referenced VLANs" in bad.stderr
        assert cli("s1", "show running-config").stdout == before and ping()
        print("PASS: invalid candidate leaves configuration and forwarding intact", flush=True)
        # Both ends down simulates a physical cable cut on the active root link.
        began = time.monotonic()
        ns("s1", "ip", "link", "set", "eth1", "down")
        ns("s2", "ip", "link", "set", "eth1", "down")
        wait_for("traffic recovers over alternate path", ping, seconds=8)
        elapsed = time.monotonic() - began
        assert elapsed < 8, elapsed
        print(f"Measured carrier-down to first successful ping: {elapsed:.3f}s", flush=True)
        wait_for("alternate port becomes forwarding", lambda: not blocked())
        # A real supervisor restart must recreate its OVS state from startup text.
        services["s1"].terminate()
        services["s1"].wait(timeout=10)
        services["s1"] = start_service("s1")
        assert cli("s1", "show running-config").stdout == before
        wait_for("saved startup configuration restores forwarding after service restart", ping)
        wait_for("saved LLDP configuration restores discovery after service restart",
                 lambda: "Device ID" in cli("s1", "show lldp neighbors").stdout, seconds=65)
        # Loss of ovs-vswitchd must cause the supervisor to shut down data ports.
        ns("s3", "ovs-appctl", "-t", "/run/ll2s/s3/ovs/ovs-vswitchd.ctl", "exit")
        wait_for("supervisor detects switch daemon loss", lambda: services["s3"].poll() is not None)
        assert services["s3"].returncode != 0
        assert all("UP" not in x["flags"] for x in json.loads(ns("s3", "ip", "-j", "link", "show").stdout) if x["ifname"] in ("eth1", "eth2"))
        print("PASS: daemon loss leaves data ports administratively down", flush=True)
        services["s3"] = start_service("s3")
        wait_for("forwarding restores after switch daemon failure and restart", ping)
        ns("s3", "ovs-appctl", "-t", "/run/ll2s/s3/ovs/ovsdb-server.ctl", "exit")
        wait_for("supervisor detects database daemon loss", lambda: services["s3"].poll() is not None)
        assert services["s3"].returncode != 0
        assert all("UP" not in x["flags"] for x in json.loads(ns("s3", "ip", "-j", "link", "show").stdout) if x["ifname"] in ("eth1", "eth2"))
        print("PASS: database loss also leaves data ports down", flush=True)
        print("ALL RSTP INTEGRATION CHECKS PASSED", flush=True)
    except Exception:
        for name in services:
            print(f"DIAGNOSTICS {name}", flush=True)
            print(cli(name, "show spanning-tree", okay=False).stdout, flush=True)
            print(ns(name, "ip", "-br", "link", okay=False).stdout, flush=True)
            print(Path(f"/tmp/{name}.log").read_text()[-6000:], flush=True)
        raise
    finally:
        for name, process in services.items():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if process.returncode not in (0, None):
                print(f"{name} supervisor log:\n" + Path(f"/tmp/{name}.log").read_text(), flush=True)


if __name__ == "__main__":
    main()
