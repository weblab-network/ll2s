"""Validated configuration and replayable CLI serialization."""

from dataclasses import asdict, dataclass, field
import re
import ipaddress
import shlex


class ConfigError(ValueError):
    pass


def number(value, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"expected an integer between {low} and {high}") from None
    if not low <= n <= high:
        raise ConfigError(f"expected an integer between {low} and {high}")
    return n


def ifname(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}", value) or value == "lo":
        raise ConfigError("invalid interface name (1–15 characters; loopback excluded)")
    return value


def vlan_list(value):
    result = set()
    for part in value.split(","):
        bounds = part.split("-")
        if len(bounds) == 1:
            result.add(number(bounds[0], 1, 4094))
        elif len(bounds) == 2:
            lo, hi = (number(x, 1, 4094) for x in bounds)
            if hi < lo:
                raise ConfigError("VLAN range must be ascending")
            result.update(range(lo, hi + 1))
        else:
            raise ConfigError("use VLAN IDs or ranges, e.g. 10,20-30")
    return sorted(result)


def vlan_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 32 or not value.isprintable() or not value.strip():
        raise ConfigError("VLAN name must contain 1–32 printable characters and cannot be blank")
    return value


def management_address(value):
    try:
        address = ipaddress.IPv4Interface(value)
        if "/" not in value or address.ip.is_unspecified or address.ip.is_multicast or address.ip.is_loopback or address.ip.is_link_local:
            raise ValueError()
        if address.network.prefixlen < 31 and address.ip in (address.network.network_address, address.network.broadcast_address):
            raise ValueError()
        return str(address)
    except ValueError:
        raise ConfigError("use a unicast management IPv4 address/prefix, e.g. 192.0.2.10/24") from None


def snmp_text(value):
    # Net-SNMP consumes a separate configuration language. Reject escapes and
    # comments as well as control characters instead of relying on shell quoting.
    if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 or ord(c) > 126 or c in '\"\\#' for c in value):
        raise ConfigError('SNMP text: at most 128 ASCII characters; no quotes, backslashes or #')
    return value


@dataclass
class Port:
    mode: str = "access"
    access: int = 1
    native: int = 1
    allowed: list[int] = field(default_factory=lambda: [1])
    shutdown: bool = False
    edge: bool = False
    bpduguard: bool = False
    cost: int = 0
    lldp_transmit: bool = True
    lldp_receive: bool = True


@dataclass
class Config:
    hostname: str = "ll2s"
    protocol: str = "stp"
    priority: int = 32768
    vlans: list[int] = field(default_factory=lambda: [1])
    ports: dict[str, Port] = field(default_factory=dict)
    vlan_names: dict[int, str] = field(default_factory=dict)
    lldp: bool = False
    management_vlan: int = 1
    management_address: str = ""
    management_gateway: str = ""
    snmp_community: str = ""
    snmp_source: str = "0.0.0.0/0"
    snmp_contact: str = ""
    snmp_location: str = ""

    def validate(self):
        number(self.management_vlan, 1, 4094)
        if self.management_address:
            management_address(self.management_address)
            if self.management_vlan not in self.vlans:
                raise ConfigError("declare the management VLAN first")
        if self.management_gateway:
            try:
                gateway = ipaddress.IPv4Address(self.management_gateway)
                address = ipaddress.IPv4Interface(self.management_address)
                if gateway not in address.network or gateway == address.ip or gateway.is_multicast or gateway.is_unspecified:
                    raise ValueError()
                if address.network.prefixlen < 31 and gateway in (address.network.network_address, address.network.broadcast_address):
                    raise ValueError()
            except ValueError:
                raise ConfigError("default gateway must be another unicast address in the management subnet") from None
        for value in (self.snmp_contact, self.snmp_location):
            snmp_text(value)
        if self.snmp_community:
            if not re.fullmatch(r"[a-zA-Z0-9_.@+-]{1,64}", self.snmp_community):
                raise ConfigError("community must be 1–64 letters, digits or _.@+-")
            if not self.management_address:
                raise ConfigError("SNMP requires a management IP address")
        try:
            ipaddress.IPv4Network(self.snmp_source)
        except ValueError:
            raise ConfigError("SNMP source must be an IPv4 network/prefix") from None
        if type(self.lldp) is not bool:
            raise ConfigError("lldp must be boolean")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", self.hostname):
            raise ConfigError("hostname must contain 1–63 letters, digits or hyphens")
        if self.protocol not in ("stp", "rstp"):
            raise ConfigError("supported spanning-tree modes: stp, rstp")
        number(self.priority, 0, 61440)
        if self.priority % 4096:
            raise ConfigError("bridge priority must be a multiple of 4096")
        for vid in self.vlans:
            number(vid, 1, 4094)
        if 1 not in self.vlans:
            raise ConfigError("VLAN 1 must exist")
        for vid, name in self.vlan_names.items():
            if vid not in self.vlans:
                raise ConfigError(f"VLAN {vid}: name references an undeclared VLAN")
            vlan_name(name)
        for name, p in self.ports.items():
            if type(p.lldp_transmit) is not bool or type(p.lldp_receive) is not bool:
                raise ConfigError("LLDP transmit/receive must be boolean")
            ifname(name)
            if name in ("br0", "ll2s-mgmt"):
                raise ConfigError("br0 and ll2s-mgmt are reserved internal interfaces")
            if p.mode not in ("access", "trunk"):
                raise ConfigError("switchport mode must be access or trunk")
            for vid in [p.access, p.native, *p.allowed]:
                number(vid, 1, 4094)
            used = [p.access] if p.mode == "access" else p.allowed
            if not set(used) <= set(self.vlans):
                raise ConfigError(f"{name}: declare referenced VLANs with 'vlan <id/range>' first")
            number(p.cost, 0, 200000000)
            if self.protocol == "stp" and p.cost > 65535:
                raise ConfigError(f"{name}: STP path cost must be 0–65535; RSTP supports long path costs")
            if self.protocol == "stp" and (p.edge or p.bpduguard):
                raise ConfigError(f"{name}: portfast and bpduguard require rstp in this release")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["ports"] = {k: Port(**v) for k, v in data.get("ports", {}).items()}
        data["vlan_names"] = {number(k, 1, 4094): v for k, v in data.get("vlan_names", {}).items()}
        result = cls(**data)
        result.validate()
        return result

    def render(self):
        lines = [f"hostname {self.hostname}", f"spanning-tree mode {self.protocol}",
                 f"spanning-tree priority {self.priority}",
                 "vlan " + ",".join(map(str, sorted(set(self.vlans))))]
        lines.append("lldp run" if self.lldp else "no lldp run")
        for vid, name in sorted(self.vlan_names.items()):
            lines.append(f"vlan {vid} name {shlex.quote(name)}")
        lines.append(f"management vlan {self.management_vlan}")
        if self.management_address:
            lines.append(f"management ip address {self.management_address}")
        if self.management_gateway:
            lines.append(f"ip default-gateway {self.management_gateway}")
        if self.snmp_community:
            lines.append(f"snmp-server community {self.snmp_community} ro")
        lines.append(f"snmp-server source {self.snmp_source}")
        for key in ("contact", "location"):
            if getattr(self, "snmp_" + key):
                lines.append(f"snmp-server {key} {shlex.quote(getattr(self, 'snmp_' + key))}")
        for name, p in sorted(self.ports.items()):
            lines.extend([f"interface {name}", f" switchport mode {p.mode}",
                          f" switchport access vlan {p.access}",
                          f" switchport trunk native vlan {p.native}",
                          " switchport trunk allowed vlan " + ",".join(map(str, p.allowed)),
                          f" spanning-tree cost {p.cost}",
                          " lldp transmit" if p.lldp_transmit else " no lldp transmit",
                          " lldp receive" if p.lldp_receive else " no lldp receive",
                          " spanning-tree portfast" if p.edge else " no spanning-tree portfast",
                          " spanning-tree bpduguard enable" if p.bpduguard else " no spanning-tree bpduguard",
                          " shutdown" if p.shutdown else " no shutdown", "exit"])
        return "\n".join(lines) + "\n"
