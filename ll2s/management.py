"""One owned IPv4 management endpoint on an OVS access VLAN."""

import json
import ipaddress

from .backend import PreflightError, run

INTERFACE = "ll2s-mgmt"


def preflight(backend, config, links, ports, membership):
    if INTERFACE in links or INTERFACE in ports:
        port = ports.get(INTERFACE, {})
        interfaces = backend.rows("Interface", "name,type")
        internal = any(x["name"] == INTERFACE and x["type"] == "internal" for x in interfaces)
        if (membership.get(INTERFACE) != backend.bridge or
                port.get("external_ids", {}).get("ll2s-owner") != backend.owner or not internal):
            raise PreflightError("ll2s-mgmt already exists and is not an owned internal management port")
    if config.management_gateway:
        routes = json.loads(run("ip", "-j", "-4", "route", "show", "default"))
        if any(r.get("dev") != INTERFACE for r in routes):
            raise PreflightError("an existing default route belongs to another interface; remove it explicitly first")


def transaction(backend, config, members):
    local = ["--", "clear", "Port", backend.bridge, "tag", "trunks", "vlan_mode"]
    if not config.management_address:
        return local + (["--", "del-port", backend.bridge, INTERFACE] if INTERFACE in members else [])
    # Isolate the implicit bridge-local interface from VLAN 1. Linux's weak-host
    # IP model could otherwise answer for the management IP on the wrong VLAN.
    # VLAN 4095 is never accepted by LL2S data-port configuration (1..4094).
    local += ["--", "set", "Port", backend.bridge, "tag=4095", "vlan_mode=access"]
    # Stable across bridge/container recreation without an extra saved state file.
    mac = "02:4c:" + ":".join(f"{b:02x}" for b in ipaddress.IPv4Interface(config.management_address).ip.packed)
    return local + ["--", "--may-exist", "add-port", backend.bridge, INTERFACE,
            "--", "set", "Interface", INTERFACE, "type=internal", f'mac="{mac}"',
            "--", "clear", "Port", INTERFACE, "tag", "trunks", "other_config",
            "--", "set", "Port", INTERFACE, f"external_ids:ll2s-owner={backend.owner}",
            "vlan_mode=access", f"tag={config.management_vlan}",
            "other_config:stp-enable=false", "other_config:rstp-enable=false"]


def apply(config):
    if not config.management_address:
        return
    # This is exclusively our internal interface, never a physical switch port.
    run("ip", "link", "set", "dev", INTERFACE, "down")
    run("ip", "-4", "route", "flush", "dev", INTERFACE)
    run("ip", "-4", "address", "flush", "dev", INTERFACE)
    run("ip", "address", "add", config.management_address, "dev", INTERFACE)
    run("ip", "link", "set", "dev", INTERFACE, "up")
    if config.management_gateway:
        run("ip", "route", "add", "default", "via", config.management_gateway, "dev", INTERFACE)


def show(config):
    if not config.management_address:
        return "Management IPv4 is not configured."
    return (f"Management VLAN: {config.management_vlan}\n"
            f"Configured IPv4: {config.management_address}\n"
            f"Default gateway: {config.management_gateway or '-'}\n" +
            run("ip", "-br", "address", "show", "dev", INTERFACE) +
            run("ip", "-4", "route", "show", "dev", INTERFACE))
