"""Real OVS/kernel checks inside a disposable LL2S container."""

import json
import subprocess

from ll2s.backend import OVSBridge


def run(*args, okay=True):
    p = subprocess.run(args, text=True, capture_output=True, timeout=20)
    if okay and p.returncode:
        raise AssertionError(f"{args}: {p.stdout} {p.stderr}")
    return p


def cli(*commands, okay=True):
    args = ["ll2sh"]
    for command in commands:
        args += ["-c", command]
    return run(*args, okay=okay)


def port():
    return next(x for x in ovs.rows("Port", "name,other_config,tag,trunks,vlan_mode") if x["name"] == "eth1")


def link():
    return json.loads(run("ip", "-j", "link", "show", "eth1").stdout)[0]


ovs = OVSBridge()
run("ip", "link", "add", "eth1", "type", "veth", "peer", "name", "peer1")
run("ip", "link", "set", "peer1", "up")
cli("conf t", "spanning-tree mode rstp", "interface eth1", "spanning-tree cost 1234", "exit", "commit", "end", "write memory")
assert port()["other_config"]["rstp-path-cost"] == "1234"
cli("conf t", "interface eth1", "spanning-tree cost 0", "exit", "commit")
assert "rstp-path-cost" not in port()["other_config"]
print("PASS: manual RSTP path cost resets to automatic", flush=True)
assert "RSTP (Open vSwitch)" in cli("show spanning-tree").stdout
cli("conf t", "spanning-tree mode stp", "commit")
assert "STP (Open vSwitch)" in cli("show spanning-tree").stdout
cli("conf t", "spanning-tree mode rstp", "commit")
print("PASS: RSTP/STP/RSTP mode changes on Linux 6.1", flush=True)

cli("conf t", "vlan 10,20", "interface eth1", "switchport mode trunk", "switchport trunk allowed vlan 10,20", "exit", "commit")
assert port()["vlan_mode"] == "trunk" and port()["tag"] == [] and port()["trunks"] == [10, 20]
cli("conf t", "interface eth1", "switchport trunk native vlan 10", "exit", "commit")
assert port()["vlan_mode"] == "native-untagged" and port()["tag"] == 10
cli("conf t", "interface eth1", "switchport mode access", "switchport access vlan 20", "exit", "commit")
assert port()["vlan_mode"] == "access" and port()["tag"] == 20 and port()["trunks"] == []
print("PASS: tagged trunk, native VLAN, and access policy replace each other cleanly", flush=True)

before_port = port()
cli("conf t", "vl 20 na USERS", "end", "wr mem")
assert port() == before_port
assert "USERS" in cli("sh vl").stdout
run("ll2sh", "--restore")
assert "vlan 20 name USERS" in cli("sh runn").stdout
assert port() == before_port
print("PASS: end commits named VLAN; save/restore preserves live access VLAN policy", flush=True)

before = cli("show running-config").stdout
bad = cli("conf t", "interface eth1", "spanning-tree bpduguard enable", "exit", "commit", okay=False)
assert bad.returncode and "BPDU guard is not supported" in bad.stderr
assert cli("show running-config").stdout == before and "UP" in link()["flags"]
print("PASS: unsupported BPDU guard rejected without interrupting the port", flush=True)

cli("conf t", "interface eth1", "spanning-tree portfast", "shutdown", "exit", "commit")
assert port()["other_config"]["rstp-port-admin-edge"] == "true"
assert "UP" not in link()["flags"]
cli("conf t", "interface eth1", "no shutdown", "no spanning-tree portfast", "exit", "commit")
assert "UP" in link()["flags"] and port()["other_config"]["rstp-port-admin-edge"] == "false"
print("PASS: edge-port configuration and administrative shutdown", flush=True)

# This external port must be detected without a rollback disturbing eth1.
run("ip", "link", "add", "foreign", "type", "dummy")
ovs.vsctl("add-port", "br0", "foreign")
bad = cli("conf t", "commit", okay=False)
assert bad.returncode and "unmanaged ports" in bad.stderr
assert "UP" in link()["flags"]
ovs.vsctl("del-port", "br0", "foreign")
print("PASS: external OVS membership is rejected before mutation", flush=True)

cli("conf t", "no interface eth1", "commit")
assert "master" not in link() and "UP" not in link()["flags"]
print("PASS: removed switch port is detached and down", flush=True)

session = subprocess.Popen(["ll2sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
try:
    assert "LL2S" in session.stdout.readline()
    result = cli("show running-config", okay=False)
    assert result.returncode and "configuration lock" in result.stderr
finally:
    session.communicate("exit\n", timeout=5)
print("PASS: concurrent CLI configuration locking", flush=True)

# Management must work with normal container caps and read-only /proc/sys.
cli("conf t", "management ip address 192.0.2.10/24", "snmp-server community lab-test ro", "end", "wr")
assert "192.0.2.10/24" in cli("show management").stdout
run("ll2sh", "--restore")
assert "snmp-server community lab-test ro" in cli("show running-config").stdout
cli("conf t", "no snmp-server community", "no management ip address", "end")
assert run("ip", "link", "show", "ll2s-mgmt", okay=False).returncode
print("PASS: management create/save/restore/remove with normal container capabilities", flush=True)
