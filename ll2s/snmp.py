"""Read-only SNMPv2c using a separately supervised Net-SNMP agent."""

import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time

from . import __version__
from .config import Config
from .lldp import clean, fingerprint
from .storage import atomic_write


def policy(config):
    return {key: getattr(config, key) for key in (
        "hostname", "management_address", "snmp_community", "snmp_source",
        "snmp_contact", "snmp_location")}


def daemon_config(config, runtime):
    config.validate()
    address = ipaddress.IPv4Interface(config.management_address).ip
    # Explicit v2c VACM group: rocommunity would also enable SNMPv1.
    return f'''agentaddress udp:{address}:161
sysName {config.hostname}
sysDescr LL2S {__version__} Linux Layer 2 Switch
sysServices 2
sysContact {config.snmp_contact or 'unset'}
sysLocation {config.snmp_location or 'unset'}
com2sec monitor {config.snmp_source} {config.snmp_community}
group readonly v2c monitor
view monitoring included .1.3.6.1.2.1.1
view monitoring included .1.3.6.1.2.1.2
view monitoring included .1.3.6.1.2.1.31
view monitoring included .1.0.8802.1.1.2
access readonly "" v2c noauth exact monitoring none none
master agentx
agentXSocket {Path(runtime) / 'agentx.sock'}
agentXPerms 0600
'''


class SnmpService:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.process = None
        self.desired = None
        self.phase = "disabled"
        self.retry = 0
        self.next_poll = 0

    def status(self, state, error=""):
        self.phase = state
        atomic_write(self.runtime / "snmp-status.json", json.dumps({
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
                print(f"SNMP shutdown error: {error}", flush=True)
            self.process = None
        for name in ("agentx.sock", "snmpd.conf"):
            try:
                (self.runtime / name).unlink(missing_ok=True)
            except OSError:
                pass

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
            if not config.snmp_community or (self.runtime / "FAILED").exists():
                self.stop()
                if changed or self.phase != "disabled":
                    self.status("disabled")
                return
            if self.process is not None and self.process.poll() is not None:
                raise OSError(f"snmpd exited ({self.process.returncode})")
            if self.process is None:
                if now < self.retry:
                    return
                atomic_write(self.runtime / "snmpd.conf", daemon_config(config, self.runtime))
                persistent = self.runtime / "snmp-state"
                persistent.mkdir(mode=0o700, exist_ok=True)
                self.process = subprocess.Popen([
                    "snmpd", "-f", "-Lo", "-C", "-c", str(self.runtime / "snmpd.conf"),
                    "-p", str(self.runtime / "snmpd.pid")],
                    env={**os.environ, "MIBS": "", "SNMP_PERSISTENT_DIR": str(persistent)},
                    start_new_session=True)
                self.status("starting")
            elif (self.runtime / "agentx.sock").exists() and self.phase != "running":
                self.status("running")
        except (OSError, ValueError) as error:
            self.stop()
            self.retry = time.monotonic() + 5
            try:
                self.status("unavailable", str(error))
            except OSError:
                pass
            print(f"SNMP unavailable (switching continues): {error}", flush=True)


def show(runtime, config):
    if not config.snmp_community:
        return "SNMP is disabled. Configure 'snmp-server community <community> ro' and commit."
    try:
        status = json.loads((Path(runtime) / "snmp-status.json").read_text())
        state = status.get("state", "unavailable")
        if status.get("policy") != fingerprint(policy(config)):
            state = "pending"
        message = clean(status.get("error", ""))
    except (OSError, ValueError):
        state, message = "pending", "waiting for supervisor"
    address = ipaddress.IPv4Interface(config.management_address).ip
    return (f"SNMPv2c read-only: {state}\nListen: {address}:161/udp\n"
            f"Allowed source: {config.snmp_source}\n"
            "MIBs: system, interfaces, ifXTable; LLDP when discovery is enabled\n" + message)
