"""Non-critical LLDP service and structured neighbor displays.

The supervisor reconciles committed intent. Discovery failures never propagate
to the switching service. Only managed, administratively enabled ports participate.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import time

from . import __version__
from .backend import BackendError, run
from .config import Config, ConfigError, ifname
from .storage import atomic_write


def policy(config):
    return {"enabled": config.lldp, "hostname": config.hostname,
            "snmp": bool(config.snmp_community),
            "ports": {name: ("disabled" if p.shutdown or not (p.lldp_transmit or p.lldp_receive)
                             else "rx-and-tx" if p.lldp_transmit and p.lldp_receive
                             else "tx-only" if p.lldp_transmit else "rx-only")
                      for name, p in sorted(config.ports.items())}}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def client(runtime, *args):
    return run("lldpcli", "-u", str(Path(runtime) / "lldp.sock"), *args, timeout=2)


def interfaces(document):
    return [port for group in document.get("lldp", []) for port in group.get("interface", [])]


def query(runtime, topic):
    try:
        return interfaces(json.loads(client(runtime, "-f", "json0", "show", topic, "details")))
    except (ValueError, TypeError, AttributeError) as error:
        raise BackendError("invalid structured response from lldpd") from error


class DiscoveryService:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.process = None
        self.desired = None
        self.phase = "disabled"
        self.next_poll = 0
        self.deadline = 0
        self.retry = 0

    def status(self, state, error=""):
        atomic_write(self.runtime / "lldp-status.json", json.dumps({
            "state": state, "error": error, "policy": fingerprint(self.desired),
            "pid": self.process.pid if self.process else None}))

    def stop(self):
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError) as error:
                print(f"LLDP shutdown error: {error}", flush=True)
            self.process = None
        try:
            (self.runtime / "lldp.sock").unlink(missing_ok=True)
        except OSError as error:
            print(f"LLDP socket cleanup error: {error}", flush=True)

    def tick(self):
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + 0.5
        try:
            state = self.runtime / "running.json"
            config = Config.from_dict(json.loads(state.read_text())) if state.exists() else Config()
            desired = policy(config)
            changed = desired != self.desired
            if changed:
                self.stop()
                self.desired = desired
                self.retry = 0
            if not desired["enabled"] or (self.runtime / "FAILED").exists():
                if changed or self.phase != "disabled":
                    self.stop()
                    self.phase = "disabled"
                    self.status("disabled")
                return
            if self.process is not None and self.process.poll() is not None:
                raise BackendError(f"lldpd exited ({self.process.returncode})")
            if self.process is None:
                if now < self.retry:
                    return
                # Start with no permitted interfaces: no advertisements escape
                # before identity and per-port policy have been installed.
                self.process = subprocess.Popen([
                    "lldpd", "-dd", "-i", "-S", f"LL2S {__version__} Linux Layer 2 Switch",
                    "-I", "!*", "-C", ",".join(["!*"] + ["!!" + n for n in desired["ports"]]), "-L", "",
                    "-u", str(self.runtime / "lldp.sock"),
                    "-p", str(self.runtime / "lldp.pid")] +
                    (["-x", "-X", str(self.runtime / "agentx.sock")] if desired["snmp"] else []),
                    start_new_session=True)
                self.phase = "starting"
                self.deadline = now + 40
                self.status("starting")
                return
            if self.phase == "starting":
                try:
                    # Set hostname first: otherwise lldpd may repeatedly block
                    # resolving the container hostname on isolated lab networks.
                    client(self.runtime, "configure", "system", "hostname", desired["hostname"])
                except BackendError:
                    if now < self.deadline:
                        return
                    raise
                commands = [
                    ("configure", "lldp", "status", "disabled"),
                    ("configure", "system", "description", f"LL2S {__version__} Linux Layer 2 Switch"),
                    ("configure", "system", "capabilities", "enabled", "bridge"),
                    ("configure", "lldp", "portidsubtype", "ifname"),
                    ("configure", "system", "interface", "pattern",
                     ",".join(["!*"] + ["!!" + name for name, mode in desired["ports"].items()
                                         if mode != "disabled"])),
                ]
                for command in commands:
                    client(self.runtime, *command)
                self.phase = "ports"
            if self.phase == "ports":
                active = {name: mode for name, mode in desired["ports"].items() if mode != "disabled"}
                present = {p["name"] for p in query(self.runtime, "interfaces")}
                if not set(active) <= present:
                    if now < self.deadline:
                        return
                    raise BackendError("managed LLDP interfaces did not become available")
                for name, mode in active.items():
                    client(self.runtime, "configure", "ports", name, "lldp", "status", mode)
                client(self.runtime, "update")
                self.phase = "running"
                self.status("running")
        except (BackendError, OSError, ValueError) as error:
            self.stop()
            self.phase = "unavailable"
            self.retry = time.monotonic() + 5
            try:
                self.status("unavailable", str(error))
            except OSError:
                pass
            print(f"LLDP unavailable (switching continues): {error}", flush=True)


def clean(value):
    """Never render control sequences supplied by a remote neighbor."""
    return "".join(c if c.isprintable() else "?" for c in str(value))[:512]


def first(record, key):
    values = record.get(key, [])
    return values[0] if values else {}


def value(record, key):
    return clean(first(record, key).get("value", "-"))


def show(runtime, topic, config):
    tokens = topic.split()
    selected = None
    if "interface" in tokens:
        index = tokens.index("interface")
        if len(tokens) > index + 1:
            selected = ifname(tokens[index + 1])
            if selected not in config.ports:
                raise ConfigError("interface is not configured")
    if not config.lldp:
        return "LLDP is disabled. Enable with 'lldp run' and commit."
    try:
        status = json.loads((Path(runtime) / "lldp-status.json").read_text())
        if status.get("policy") != fingerprint(policy(config)):
            return "LLDP configuration pending; supervisor is reconciling committed changes."
        if status.get("state") != "running":
            return "LLDP " + clean(status.get("state", "unavailable")) + ": " + clean(status.get("error") or "waiting for discovery service")
        records = query(runtime, "interfaces" if tokens[1] == "interface" else "neighbors")
    except (BackendError, OSError, ValueError) as error:
        return "LLDP unavailable: " + clean(error)
    if tokens[1] == "interface":
        live = {p["name"]: p for p in records}
        rows = ["LLDP enabled (lldpd)", f"{'Interface':<16} {'Transmit':<10} {'Receive':<10} Status"]
        for name, port in sorted(config.ports.items()):
            if selected and name != selected:
                continue
            mode = policy(config)["ports"][name]
            operational = "disabled" if mode == "disabled" else value(live[name], "status") if name in live else "unavailable"
            rows.append(f"{name:<16} {str(port.lldp_transmit):<10} {str(port.lldp_receive):<10} {operational}")
        return "\n".join(rows)
    records = [r for r in records if r.get("name") in config.ports
               and config.ports[r["name"]].lldp_receive
               and not config.ports[r["name"]].shutdown
               and (selected is None or r.get("name") == selected)]
    if not records:
        return "No LLDP neighbors."
    detail = tokens[-1] == "detail"
    rows = [] if detail else [f"{'Device ID':<24} {'Local Intf':<16} {'Capability':<16} Port ID"]
    for r in records:
        chassis, port = first(r, "chassis"), first(r, "port")
        name, local, remote = value(chassis, "name"), clean(r.get("name", "-")), value(port, "id")
        caps = ",".join(clean(c.get("type", "?")) for c in chassis.get("capability", [])
                        if c.get("enabled") is True or c.get("enabled") == "on") or "-"
        if detail:
            rows += [f"Local interface: {local}", f"System name: {name}",
                     f"Chassis ID: {value(chassis, 'id')}", f"Port ID: {remote}",
                     f"Port description: {value(port, 'descr')}", f"Capabilities: {caps}",
                     f"System description: {value(chassis, 'descr')}",
                     "Management addresses: " + (", ".join(clean(a.get("value", "-")) for a in chassis.get("mgmt-ip", [])) or "-"),
                     f"Advertised TTL: {value(port, 'ttl')} seconds", f"Age: {clean(r.get('age', '-'))}", ""]
        else:
            rows.append(f"{name[:23]:<24} {local:<16} {caps[:15]:<16} {remote}")
    return "\n".join(rows).rstrip()
