"""Real management VLAN and SNMP checks in disposable network namespaces."""

import json
import os
from pathlib import Path
import signal

from integration import run, ns, cli, wait_for, link, start_service


def get(host="h1", community="lab-test", version="2c", oid=".1.3.6.1.2.1.1.5.0", target="192.0.2.10"):
    return ns(host, "snmpget", "-v", version, "-c", community, "-t", "0.5", "-r", "0",
              "-Oqv", target, oid, okay=False)


def main():
    if os.environ.get("LL2S_DISPOSABLE_TEST") != "1":
        raise SystemExit("This requires an isolated disposable privileged container")
    services = {}
    for name in ("s1", "s2", "h1", "h2", "h3"):
        run("ip", "netns", "add", name)
        ns(name, "ip", "link", "set", "lo", "up")
    link("s1", "eth1", "h1", "eth0")
    link("s1", "eth2", "h2", "eth0")
    link("s1", "eth3", "s2", "eth1")
    link("s2", "eth2", "h3", "eth0")
    try:
        for name in ("s1", "s2"):
            services[name] = start_service(name)
        for host, address in (("h1", "192.0.2.1/24"), ("h2", "192.0.2.2/24"), ("h3", "198.51.100.1/24")):
            ns(host, "ip", "addr", "add", address, "dev", "eth0")
            ns(host, "ip", "link", "set", "eth0", "up")
        cli("s1", "conf t", "hostname SW1", "spanning-tree mode rstp", "vlan 10,20", "lldp run",
            "interface eth1", "switchport access vlan 10", "spanning-tree portfast", "exit",
            "interface eth2", "switchport access vlan 20", "spanning-tree portfast", "exit",
            "interface eth3", "switchport mode trunk", "switchport trunk allowed vlan 10,20", "exit",
            "management vlan 10", "management ip address 192.0.2.10/24", "ip default-gateway 192.0.2.1",
            "snmp-server community lab-test ro", "snmp-server contact 'Lab Operator'",
            "snmp-server location 'Rack 1'", "end", "wr")
        cli("s2", "conf t", "hostname SW2", "spanning-tree mode rstp", "vlan 10,20", "lldp run",
            "interface eth1", "switchport mode trunk", "switchport trunk allowed vlan 10,20", "exit",
            "interface eth2", "switchport access vlan 10", "spanning-tree portfast", "exit", "end")
        wait_for("management address reachable on VLAN 10", lambda: get().returncode == 0, seconds=45)
        assert "SW1" in get().stdout
        assert "Lab Operator" in get(oid=".1.3.6.1.2.1.1.4.0").stdout
        assert get(community="wrong").returncode
        assert get(version="1").returncode
        assert get(host="h2").returncode
        result = ns("h1", "snmpset", "-v2c", "-c", "lab-test", "-t", "1", "-r", "0",
                    "192.0.2.10", ".1.3.6.1.2.1.1.6.0", "s", "changed", okay=False)
        assert result.returncode, result.stdout
        assert "Rack 1" in get(oid=".1.3.6.1.2.1.1.6.0").stdout
        print("PASS: v2c identity/contact/location; wrong community, v1, SET and other VLAN rejected", flush=True)
        cli("s1", "conf t", "interface eth2", "switchport access vlan 1", "end")
        wait_for("management resumes after VLAN change", lambda: get().returncode == 0)
        assert get(host="h2").returncode, "management leaked through the implicit bridge-local VLAN 1 port"
        print("PASS: management is isolated from VLAN 1 as well as VLAN 20", flush=True)
        # Different source subnet: replies must use the configured default gateway.
        ns("h1", "ip", "addr", "add", "198.51.100.254/24", "dev", "eth0")
        ns("h1", "sysctl", "-w", "net.ipv4.ip_forward=1")
        ns("h3", "ip", "route", "add", "default", "via", "198.51.100.254")
        wait_for("SNMP reaches a routed monitoring subnet through management gateway",
                 lambda: get(host="h3").returncode == 0)
        cli("s1", "conf t", "snmp-server source 192.0.2.0/24", "end")
        wait_for("source restriction applied", lambda: "running" in cli("s1", "show snmp").stdout)
        assert get(host="h3").returncode
        wait_for("allowed source remains reachable after commit", lambda: get().returncode == 0)
        print("PASS: source network restriction", flush=True)
        cli("s1", "conf t", "no snmp-server source", "end", "wr")
        wait_for("SNMP policy running", lambda: "running" in cli("s1", "show snmp").stdout)
        wait_for("polling resumes after full-port commit", lambda: get().returncode == 0)
        index = json.loads(ns("s1", "ip", "-j", "link", "show", "eth1").stdout)[0]["ifindex"]
        assert "eth1" in get(oid=f".1.3.6.1.2.1.31.1.1.1.1.{index}").stdout
        counter = f".1.3.6.1.2.1.31.1.1.1.6.{index}"
        before = int(get(oid=counter).stdout)
        ns("h1", "ping", "-c", "5", "-s", "1000", "192.0.2.10")
        wait_for("ifHCInOctets increases with traffic", lambda: int(get(oid=counter).stdout) > before, seconds=15)
        def lldp_names():
            return ns("h1", "snmpwalk", "-v2c", "-c", "lab-test", "-t", "1", "-r", "0", "-On",
                      "192.0.2.10", ".1.0.8802.1.1.2.1.4.1.1.9", okay=False).stdout
        wait_for("LLDP-MIB exposes SW2 neighbor via AgentX", lambda: '"SW2"' in lldp_names(), seconds=65)
        portid = ns("h1", "snmpwalk", "-v2c", "-c", "lab-test", "-On", "192.0.2.10",
                    ".1.0.8802.1.1.2.1.3.7.1.3").stdout
        eth3index = json.loads(ns("s1", "ip", "-j", "link", "show", "eth3").stdout)[0]["ifindex"]
        assert f'.{eth3index} = STRING: "eth3"' in portid, portid
        status_path = Path("/run/ll2s/s1/snmp-status.json")
        oldpid = json.loads(status_path.read_text())["pid"]
        os.kill(oldpid, signal.SIGKILL)
        assert ns("h1", "ping", "-c", "1", "-W", "1", "198.51.100.1", okay=False).returncode == 0
        wait_for("SNMP restarts independently after daemon failure", lambda: get().returncode == 0)
        wait_for("LLDP AgentX reconnects", lambda: '"SW2"' in lldp_names(), seconds=65)
        saved = cli("s1", "show startup-config").stdout
        oldmac = json.loads(ns("s1", "ip", "-j", "link", "show", "ll2s-mgmt").stdout)[0]["address"]
        services["s1"].terminate()
        services["s1"].wait(timeout=10)
        services["s1"] = start_service("s1")
        assert cli("s1", "show running-config").stdout == saved
        assert json.loads(ns("s1", "ip", "-j", "link", "show", "ll2s-mgmt").stdout)[0]["address"] == oldmac
        # A recreated OVS bridge needs the same RSTP/ARP convergence budget as
        # initial startup; daemon readiness alone does not establish reachability.
        wait_for("saved management/SNMP restores after service restart",
                 lambda: get().returncode == 0, seconds=45)
        cli("s1", "conf t", "no snmp-server community", "no management ip address", "end")
        wait_for("SNMP disabled", lambda: json.loads(status_path.read_text())["state"] == "disabled")
        assert ns("s1", "ip", "link", "show", "ll2s-mgmt", okay=False).returncode
        assert not json.loads(ns("s1", "ip", "-j", "route", "show", "default").stdout)
        print("ALL MANAGEMENT/SNMP CHECKS PASSED", flush=True)
    except Exception:
        for name in services:
            print(Path(f"/tmp/{name}.log").read_text()[-8000:], flush=True)
        raise
    finally:
        for process in services.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
