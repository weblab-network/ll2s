"""Cisco-style local administration shell with explicit candidate commits."""

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from . import __version__
from .backend import BackendError, OVSBridge, PreflightError
from .config import Config, ConfigError, Port, ifname, number, vlan_list, vlan_name, management_address, snmp_text
from .commands import resolve, suggestions
from .storage import atomic_write


class Shell:
    def __init__(self, backend=None, runtime="/run/ll2s", startup="/etc/ll2s/startup.conf", out=print):
        self.backend = backend or OVSBridge(runtime)
        self.runtime = Path(runtime)
        self.startup = Path(startup)
        self.out = out
        state = self.runtime / "running.json"
        self.running = Config.from_dict(json.loads(state.read_text())) if state.exists() else Config()
        self.candidate = copy.deepcopy(self.running)
        self.mode = "exec"
        self.interface = None
        self.healthy = not (self.runtime / "FAILED").exists()

    @property
    def prompt(self):
        suffix = {"exec": "#", "config": "(config)#", "interface": "(config-if)#"}[self.mode]
        return self.candidate.hostname + suffix + " "

    def commands(self):
        common = ["show running-config", "show candidate-config", "show startup-config",
                  "show spanning-tree", "show vlan", "show interfaces", "show mac address-table",
                  "show version", "show status", "show management", "show snmp", "exit", "end", "help",
                  "write", "write memory", "copy running-config startup-config"]
        common += ["show lldp neighbors", "show lldp neighbors detail",
                   "show lldp neighbors interface <name>",
                   "show lldp neighbors interface <name> detail",
                   "show lldp interface", "show lldp interface <name>"]
        if self.mode == "exec":
            return common + ["configure terminal"]
        common += ["do " + c for c in common if c.startswith("show ")]
        common += ["commit", "abort"]
        if self.mode == "config":
            return common + ["lldp run", "no lldp run", "hostname <name>", "spanning-tree mode <stp|rstp>",
                             "spanning-tree priority <0-61440>", "vlan <ids>", "no vlan <ids>",
                             "vlan <id> name <name>", "no vlan <id> name",
                             "interface <name>", "no interface <name>",
                             "management vlan <id>", "management ip address <ipv4/prefix>",
                             "no management ip address", "ip default-gateway <ipv4>", "no ip default-gateway",
                             "snmp-server community <community> ro", "no snmp-server community",
                             "snmp-server source <network/prefix>", "no snmp-server source",
                             "snmp-server contact <text>", "no snmp-server contact",
                             "snmp-server location <text>", "no snmp-server location"]
        return common + ["lldp transmit", "no lldp transmit", "lldp receive", "no lldp receive",
                         "switchport mode <access|trunk>", "switchport access vlan <id>",
                         "switchport trunk native vlan <id>", "switchport trunk allowed vlan <ids>",
                         "shutdown", "no shutdown", "spanning-tree cost <0-200000000>",
                         "spanning-tree portfast", "no spanning-tree portfast",
                         "spanning-tree bpduguard enable", "no spanning-tree bpduguard"]

    def commit(self):
        self.candidate.validate()
        self.backend.preflight(self.candidate)
        try:
            self.backend.apply(self.candidate, self.running)
            atomic_write(self.runtime / "running.json", json.dumps(self.candidate.to_dict(), indent=2) + "\n")
        except PreflightError:
            # Nothing changed: attempting rollback could disturb an external port.
            raise
        except (BackendError, OSError) as error:
            try:
                attempted = copy.deepcopy(self.candidate)
                # Failure can occur before removed ports have been detached.
                attempted.ports.update(self.running.ports)
                self.backend.apply(self.running, attempted)
            except (BackendError, OSError) as rollback_error:
                self.healthy = False
                try:
                    self.backend.stop_forwarding()
                except BackendError as stop_error:
                    rollback_error = f"{rollback_error}; bridge shutdown also failed: {stop_error}"
                atomic_write(self.runtime / "FAILED", str(rollback_error) + "\n")
                raise BackendError(f"commit failed: {error}; rollback failed: {rollback_error}. "
                                   "Runtime state is uncertain; inspect interfaces before retrying.") from error
            raise BackendError(f"commit failed; previous configuration restored: {error}") from error
        self.running = copy.deepcopy(self.candidate)
        self.healthy = True
        (self.runtime / "FAILED").unlink(missing_ok=True)
        self.out("Commit complete. Use 'write memory' to save for restart.")

    def help(self, prefix=""):
        choices = suggestions(self.commands(), prefix)
        self.out("\n".join(choices) if choices else "% No matching commands.")

    def execute(self, line):
        line = line.strip()
        if not line or line.startswith(("!", "#")):
            return True
        if line == "help" or line.endswith("?"):
            self.help("" if line == "help" else line[:-1])
            return True
        tokens = resolve(self.commands(), shlex.split(line))
        if tokens == ["help"]:
            self.help()
            return True
        if tokens[0] == "do" and self.mode != "exec":
            if tokens[1:2] != ["show"]:
                raise ConfigError("do supports show commands")
            tokens = tokens[1:]
        if tokens[0] == "show":
            topic = " ".join(tokens[1:])
            if topic in ("running-config", "candidate-config"):
                self.out((self.running if topic == "running-config" else self.candidate).render().rstrip())
            elif topic == "startup-config":
                self.out(self.startup.read_text().rstrip() if self.startup.exists() else "No startup configuration.")
            elif topic == "version":
                self.out(f"LL2S {__version__} — Linux Layer 2 Switch")
            elif topic == "status":
                self.out("Configuration state: " + ("healthy" if self.healthy else "UNCERTAIN: last rollback failed"))
                self.out("Candidate changes: " + ("yes" if self.candidate != self.running else "no"))
            else:
                self.out(self.backend.show(topic, self.running).rstrip())
            return True
        if tokens == ["end"]:
            if self.mode != "exec" and (self.candidate != self.running or not self.healthy):
                self.commit()
            self.mode = "exec"
            self.interface = None
        elif tokens == ["exit"]:
            if self.mode == "exec":
                if self.candidate != self.running:
                    self.out("Discarding uncommitted candidate changes.")
                return False
            self.mode = "config" if self.mode == "interface" else "exec"
            self.interface = None
        elif tokens in (["configure", "terminal"], ["conf", "t"]) and self.mode == "exec":
            self.mode = "config"
            self.out("Editing candidate configuration. 'commit' applies it; 'end' applies and exits; 'abort' discards it.")
        elif tokens in (["write"], ["write", "memory"], ["copy", "running-config", "startup-config"]):
            if not self.healthy:
                raise ConfigError("cannot save uncertain runtime state; complete a successful commit first")
            if self.candidate != self.running:
                raise ConfigError("uncommitted changes; commit or abort before saving")
            atomic_write(self.startup, self.running.render())
            self.out("Startup configuration saved.")
        elif tokens == ["abort"] and self.mode != "exec":
            self.candidate = copy.deepcopy(self.running)
            self.mode, self.interface = "config", None
        elif tokens == ["commit"] and self.mode != "exec":
            self.commit()
        elif self.mode == "config":
            self.configure(tokens)
        elif self.mode == "interface":
            self.configure_port(tokens)
        else:
            raise ConfigError("unknown command; use '?' for commands in this mode")
        return True

    def configure(self, t):
        c = self.candidate
        if t[:2] == ["management", "vlan"] and len(t) == 3:
            c.management_vlan = number(t[2], 1, 4094)
        elif t[:3] == ["management", "ip", "address"] and len(t) == 4:
            c.management_address = management_address(t[3])
        elif t == ["no", "management", "ip", "address"]:
            c.management_address = ""
            c.management_gateway = ""
        elif t[:2] == ["ip", "default-gateway"] and len(t) == 3:
            c.management_gateway = t[2]
        elif t == ["no", "ip", "default-gateway"]:
            c.management_gateway = ""
        elif t[:2] == ["snmp-server", "community"] and len(t) == 4 and t[3] == "ro":
            c.snmp_community = t[2]
        elif t == ["no", "snmp-server", "community"]:
            c.snmp_community = ""
        elif t[:1] == ["snmp-server"] and len(t) == 3 and t[1] in ("source", "contact", "location"):
            setattr(c, "snmp_" + t[1], snmp_text(t[2]))
        elif t[:2] == ["no", "snmp-server"] and len(t) == 3 and t[2] in ("source", "contact", "location"):
            setattr(c, "snmp_" + t[2], "0.0.0.0/0" if t[2] == "source" else "")
        elif t in (["lldp", "run"], ["no", "lldp", "run"]):
            c.lldp = t[0] != "no"
        elif len(t) == 2 and t[0] == "hostname":
            probe = Config(hostname=t[1])
            probe.validate()
            c.hostname = t[1]
        elif len(t) == 3 and t[:2] == ["spanning-tree", "mode"] and t[2] in ("stp", "rstp"):
            c.protocol = t[2]
        elif len(t) == 3 and t[:2] == ["spanning-tree", "priority"]:
            priority = number(t[2], 0, 61440)
            if priority % 4096:
                raise ConfigError("priority must be a multiple of 4096")
            c.priority = priority
        elif len(t) == 2 and t[0] == "vlan":
            c.vlans = sorted(set(c.vlans) | set(vlan_list(t[1])))
        elif len(t) == 4 and t[0] == "vlan" and t[2] == "name":
            vid = number(t[1], 1, 4094)
            name = vlan_name(t[3])
            c.vlans = sorted(set(c.vlans) | {vid})
            c.vlan_names[vid] = name
        elif len(t) == 4 and t[:2] == ["no", "vlan"] and t[3] == "name":
            vid = number(t[2], 1, 4094)
            c.vlan_names.pop(vid, None)
        elif len(t) == 3 and t[:2] == ["no", "vlan"]:
            removed = vlan_list(t[2])
            if 1 in removed:
                raise ConfigError("VLAN 1 cannot be removed")
            c.vlans = sorted(set(c.vlans) - set(removed))
            for vid in removed:
                c.vlan_names.pop(vid, None)
        elif len(t) == 2 and t[0] == "interface":
            name = ifname(t[1])
            if name in ("br0", "ll2s-mgmt"):
                raise ConfigError("br0 and ll2s-mgmt are reserved internal interfaces")
            c.ports.setdefault(name, Port())
            self.mode, self.interface = "interface", name
        elif len(t) == 3 and t[:2] == ["no", "interface"]:
            name = ifname(t[2])
            if name not in c.ports:
                raise ConfigError("interface is not configured")
            del c.ports[name]
        else:
            raise ConfigError("unknown configuration command; use '?'")

    def configure_port(self, t):
        p = self.candidate.ports[self.interface]
        if t in (["lldp", "transmit"], ["no", "lldp", "transmit"],
                 ["lldp", "receive"], ["no", "lldp", "receive"]):
            setattr(p, "lldp_" + t[-1], t[0] != "no")
        elif len(t) == 3 and t[:2] == ["switchport", "mode"] and t[2] in ("access", "trunk"):
            p.mode = t[2]
        elif len(t) == 4 and t[:3] == ["switchport", "access", "vlan"]:
            p.access = number(t[3], 1, 4094)
        elif len(t) == 5 and t[:4] == ["switchport", "trunk", "native", "vlan"]:
            p.native = number(t[4], 1, 4094)
        elif len(t) == 5 and t[:4] == ["switchport", "trunk", "allowed", "vlan"]:
            p.allowed = vlan_list(t[4])
        elif t in (["shutdown"], ["no", "shutdown"]):
            p.shutdown = t == ["shutdown"]
        elif len(t) == 3 and t[:2] == ["spanning-tree", "cost"]:
            p.cost = number(t[2], 0, 200000000)
        elif t in (["spanning-tree", "portfast"], ["no", "spanning-tree", "portfast"]):
            p.edge = t[0] != "no"
        elif t in (["spanning-tree", "bpduguard", "enable"], ["no", "spanning-tree", "bpduguard"]):
            p.bpduguard = t[0] != "no"
        else:
            raise ConfigError("unknown interface command; use '?'")

    def load(self, content):
        # Configuration files contain only configuration syntax, never show/write/commit.
        # Parse the whole file before touching the data plane.
        self.candidate = Config()
        self.mode, self.interface = "config", None
        for lineno, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith(("!", "#")):
                continue
            try:
                t = shlex.split(line)
                if t == ["end"]:
                    self.mode, self.interface = "config", None
                elif t == ["exit"] and self.mode == "interface":
                    self.mode, self.interface = "config", None
                elif self.mode == "interface":
                    self.configure_port(t)
                else:
                    self.configure(t)
            except ValueError as e:
                raise ConfigError(f"line {lineno}: {e}") from e
        self.candidate.validate()
        self.mode, self.interface = "config", None


def main():
    parser = argparse.ArgumentParser(description="LL2S local switch CLI")
    parser.add_argument("-c", "--command", action="append", default=[])
    parser.add_argument("-f", "--file", type=Path, help="parse and apply a configuration file")
    parser.add_argument("--check", action="store_true", help="validate --file without touching Linux or runtime state")
    parser.add_argument("--restore", action="store_true", help="apply saved startup configuration")
    parser.add_argument("--runtime-dir", default=os.environ.get("LL2S_RUNTIME_DIR", "/run/ll2s"))
    parser.add_argument("--startup", default=os.environ.get("LL2S_STARTUP", "/etc/ll2s/startup.conf"))
    args = parser.parse_args()
    if args.check and not args.file:
        parser.error("--check requires --file")
    if sum(bool(x) for x in (args.file, args.restore, args.command)) > 1:
        parser.error("choose one of --file, --restore or --command")
    try:
        if args.check:
            with tempfile.TemporaryDirectory() as runtime:
                shell = Shell(runtime=runtime)
                shell.load(args.file.read_text())
            print("Configuration syntax and references are valid.")
            return 0
        runtime = Path(args.runtime_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        with (runtime / "lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError("another ll2sh session holds the configuration lock") from None
            shell = Shell(runtime=runtime, startup=args.startup)
            if args.file or args.restore:
                shell.load((args.file or Path(args.startup)).read_text())
                shell.commit()
                return 0
            if args.command:
                for command in args.command:
                    if not shell.execute(command):
                        break
                return 0
            try:
                from .interactive import LineEditor
                # Retain the object (and its C callback) until the input loop ends.
                editor = LineEditor(shell)
            except ImportError:
                editor = None
            hint = "Press '?' for help (no Enter needed)." if editor and editor.immediate_help else "Type '?' then Enter for help."
            print(f"LL2S {__version__}. {hint}")
            while True:
                try:
                    line = editor.read(shell.prompt) if editor else input(shell.prompt)
                    if not shell.execute(line):
                        break
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print()
                except (ConfigError, BackendError, OSError, ValueError) as e:
                    print(f"% {e}", file=sys.stderr)
        return 0
    except (ConfigError, BackendError, OSError, ValueError) as e:
        print(f"% {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
