"""Open vSwitch backend. OVSDB owns policy; Linux supplies the kernel datapath."""

import json
import os
import subprocess
from pathlib import Path

from .config import ConfigError, ifname
from .storage import atomic_write


class BackendError(RuntimeError):
    pass


class PreflightError(BackendError):
    """An apply attempt was rejected before any mutation."""


def run(*args, timeout=15):
    try:
        p = subprocess.run([str(x) for x in args], text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackendError(str(e)) from e
    if p.returncode:
        raise BackendError(f"{' '.join(map(str, args))}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def decode(value):
    """Decode OVSDB's JSON atoms, sets, maps and UUID references."""
    if isinstance(value, list) and len(value) == 2:
        kind, data = value
        if kind == "map":
            return {k: decode(v) for k, v in data}
        if kind == "set":
            return [decode(v) for v in data]
        if kind in ("uuid", "named-uuid"):
            return data
    return value


def as_set(value):
    return set(value if isinstance(value, list) else [value])


class OVSBridge:
    bridge = "br0"
    owner = "ll2s"

    def __init__(self, runtime=None):
        self.runtime = Path(runtime or os.environ.get("LL2S_RUNTIME_DIR", "/run/ll2s"))
        self.ovsdir = self.runtime / "ovs"

    def vsctl(self, *args):
        return run("ovs-vsctl", "--timeout=10", f"--db=unix:{self.ovsdir}/db.sock", *args)

    def appctl(self, *args):
        return run("ovs-appctl", "-t", str(self.ovsdir / "ovs-vswitchd.ctl"), *args)

    def rows(self, table, columns):
        result = json.loads(self.vsctl("--format=json", f"--columns={columns}", "list", table))
        return [dict(zip(result["headings"], map(decode, row))) for row in result["data"]]

    def links(self):
        return {x["ifname"]: x for x in json.loads(run("ip", "-j", "-d", "link", "show"))}

    def topology(self):
        bridges = {x["name"]: x for x in self.rows("Bridge", "name,ports,external_ids,stp_enable,rstp_enable")}
        ports = {x["name"]: x for x in self.rows("Port", "_uuid,name,external_ids")}
        membership = {p["name"]: b["name"] for b in bridges.values() for p in ports.values()
                      if p["_uuid"] in as_set(b["ports"])}
        return bridges, ports, membership

    def preflight(self, config):
        config.validate()
        if any(p.bpduguard for p in config.ports.values()):
            raise PreflightError("BPDU guard is not supported by the OVS backend; use 'no spanning-tree bpduguard'")
        self.appctl("version")  # Require a responding switch daemon, not just OVSDB.
        bridges, ports, membership = self.topology()
        links = self.links()
        br = bridges.get(self.bridge)
        if br and br["external_ids"].get("ll2s-owner") != self.owner:
            raise PreflightError("br0 already exists and is not an LL2S OVS bridge")
        if self.bridge in links and (not br or links[self.bridge].get("linkinfo", {}).get("info_kind") == "bridge"):
            raise PreflightError("br0 is an existing non-OVS interface; migrate it explicitly before using LL2S")
        members = {name for name, b in membership.items() if b == self.bridge and name != self.bridge}
        if any(ports[name]["external_ids"].get("ll2s-owner") != self.owner for name in members):
            raise PreflightError("unmanaged ports attached to br0; reconcile them manually first")
        from . import management
        management.preflight(self, config, links, ports, membership)
        for name in config.ports:
            if name not in links:
                raise PreflightError(f"interface {name} does not exist")
            link = links[name]
            if name in membership and membership[name] != self.bridge:
                raise PreflightError(f"{name} belongs to another OVS bridge")
            if link.get("master") is not None and not (link["master"] == "ovs-system" and name in members):
                raise PreflightError(f"{name} belongs to another bridge/bond")
            if link.get("link_type") != "ether" or link.get("linkinfo", {}).get("info_kind") in ("bridge", "openvswitch", "bond"):
                raise PreflightError(f"{name} is not a supported Ethernet switch port")
            addresses = json.loads(run("ip", "-j", "address", "show", "dev", name))
            if any(a.get("scope") != "link" for x in addresses for a in x.get("addr_info", [])):
                raise PreflightError(f"{name} has an IP address; remove it explicitly before using it as a switch port")
        return links, members

    def remember_ports(self, names, links):
        # This journal lets the supervisor stop ports even if both OVS daemons
        # are dead. Interface identity avoids touching replacement devices.
        identities = {n: [links[n]["ifindex"], links[n]["address"]] for n in names if n in links}
        atomic_write(self.runtime / "active-ports.json", json.dumps(identities))

    def stop_forwarding(self):
        journal = self.runtime / "active-ports.json"
        if not journal.exists():
            return
        links = self.links()
        errors = []
        for name, identity in json.loads(journal.read_text()).items():
            ifname(name)
            link = links.get(name)
            if link and [link["ifindex"], link.get("address")] == identity:
                try:
                    run("ip", "link", "set", "dev", name, "down")
                except BackendError as e:
                    errors.append(str(e))
        if errors:
            raise BackendError("; ".join(errors))

    def apply(self, config, previous):
        links, members = self.preflight(config)
        from . import management
        if not (members - {management.INTERFACE}) <= set(previous.ports):
            raise PreflightError("OVS membership differs from running configuration; reconcile it first")
        self.remember_ports(members | set(config.ports), links)
        self.stop_forwarding()
        # Commit policy together while every affected physical port is down.
        transaction = ["--may-exist", "add-br", self.bridge,
                       "--", "set", "Bridge", self.bridge,
                       f"external_ids:ll2s-owner={self.owner}", "datapath_type=system",
                       "fail_mode=standalone", f"stp_enable={'true' if config.protocol == 'stp' else 'false'}",
                       f"rstp_enable={'true' if config.protocol == 'rstp' else 'false'}",
                       "--", "clear", "Bridge", self.bridge, "other_config", "controller",
                       "--", "set", "Bridge", self.bridge,
                       f"other_config:{config.protocol}-priority={config.priority}"]
        transaction += management.transaction(self, config, members)
        for name in sorted(members - set(config.ports) - {management.INTERFACE}):
            transaction += ["--", "del-port", self.bridge, name]
        for name, port in config.ports.items():
            transaction += ["--", "--may-exist", "add-port", self.bridge, name,
                            "--", "clear", "Port", name, "tag", "trunks", "other_config",
                            "--", "set", "Port", name, f"external_ids:ll2s-owner={self.owner}"]
            if port.mode == "access":
                transaction += ["vlan_mode=access", f"tag={port.access}"]
            else:
                transaction += ["trunks=" + ",".join(map(str, port.allowed))]
                if port.native in port.allowed:
                    transaction += ["vlan_mode=native-untagged", f"tag={port.native}"]
                else:
                    transaction += ["vlan_mode=trunk"]
            if port.cost:
                transaction += [f"other_config:{config.protocol}-path-cost={port.cost}"]
            if config.protocol == "rstp":
                transaction += [f"other_config:rstp-port-admin-edge={str(port.edge).lower()}",
                                "other_config:rstp-port-auto-edge=false"]
        self.vsctl(*transaction)
        # ovs-vsctl may exit successfully while an Interface has ofport=-1.
        interfaces = {x["name"]: x for x in self.rows("Interface", "name,ofport,error")}
        expected = set(config.ports) | ({management.INTERFACE} if config.management_address else set())
        for name in expected:
            interface = interfaces.get(name, {})
            if interface.get("error") or not isinstance(interface.get("ofport"), int) or interface["ofport"] <= 0:
                raise BackendError(f"OVS could not attach {name}: {interface.get('error') or 'no OpenFlow port'}")
        bridge = next(x for x in self.rows("Bridge", "name,stp_enable,rstp_enable") if x["name"] == self.bridge)
        if not bridge[f"{config.protocol}_enable"] or bridge[f"{'stp' if config.protocol == 'rstp' else 'rstp'}_enable"]:
            raise BackendError("OVS spanning-tree mode readback failed; ports remain down")
        management.apply(config)
        run("ip", "link", "set", "dev", self.bridge, "up")
        for name, port in config.ports.items():
            if not port.shutdown:
                run("ip", "link", "set", "dev", name, "up")
        self.remember_ports(set(config.ports), links)

    def show(self, topic, config):
        if topic == "management":
            from .management import show
            return show(config)
        if topic == "snmp":
            from .snmp import show
            return show(self.runtime, config)
        if topic.startswith("lldp "):
            from .lldp import show
            return show(self.runtime, topic, config)
        if topic == "interfaces":
            return run("ip", "-br", "link", "show")
        if topic == "vlan":
            rows = ["Configured VLANs:", f"{'VLAN':<6} NAME"]
            rows.extend(f"{vid:<6} {config.vlan_names.get(vid, '-')}" for vid in sorted(config.vlans))
            rows.extend(["", "Live OVS port VLAN settings:",
                         self.vsctl("--columns=name,vlan_mode,tag,trunks", "list", "Port")])
            return "\n".join(rows)
        if topic == "mac address-table":
            return self.appctl("fdb/show", self.bridge)
        if topic == "spanning-tree":
            bridges, _, _ = self.topology()
            br = bridges.get(self.bridge)
            if not br:
                return "No managed bridge. Configure ports and commit first.\n"
            protocol = "rstp" if br["rstp_enable"] else "stp" if br["stp_enable"] else None
            if not protocol:
                return "Spanning tree is disabled in OVS.\n"
            return f"Protocol: {protocol.upper()} (Open vSwitch)\n" + self.appctl(f"{protocol}/show", self.bridge)
        raise ConfigError(f"unknown show topic: {topic}")
