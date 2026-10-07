"""LLDP assertions shared with the disposable RSTP triangle harness."""

import json
from pathlib import Path
import signal
import os


def check_discovery(switches, cli, ns, wait_for, ping, blocked):
    def records(name):
        result = ns(name, "lldpcli", "-u", f"/run/ll2s/{name}/lldp.sock", "-f", "json0",
                    "show", "neighbors", "details", okay=False)
        if result.returncode:
            return []
        return [p for g in json.loads(result.stdout)["lldp"] for p in g.get("interface", [])]

    def peers(name):
        return {(p["name"], p["chassis"][0]["name"][0]["value"], p["port"][0]["id"][0]["value"])
                for p in records(name)}

    expected = {"s1": {("eth1", "s2", "eth1"), ("eth2", "s3", "eth1")},
                "s2": {("eth1", "s1", "eth1"), ("eth2", "s3", "eth2")},
                "s3": {("eth1", "s1", "eth2"), ("eth2", "s2", "eth2")}}
    wait_for("LLDP discovers exactly the directly connected switch ports",
             lambda: all(peers(s) == expected[s] for s in switches), seconds=65)
    for name in switches:
        output = cli(name, "show lldp neighbors").stdout
        assert "Bridge" in output and "Device ID" in output, output
        detail = cli(name, "show lldp neighbors interface eth1 detail").stdout
        assert "Local interface: eth1" in detail and "Local interface: eth2" not in detail, detail
        assert "LL2S" in detail and "Port ID: eth" in detail, detail
    blocked_switch, blocked_port = blocked()[0]
    assert any(p[0] == blocked_port for p in peers(blocked_switch))
    print("PASS: LLDP on RSTP-discarding port; neighbor details and port IDs", flush=True)

    # Kill only the discovery daemon: switching and the supervisor must survive.
    state = Path("/run/ll2s/s1/lldp-status.json")
    old_pid = json.loads(state.read_text())["pid"]
    os.kill(old_pid, signal.SIGKILL)
    assert ping(), "LLDP daemon failure interrupted forwarding"
    wait_for("LLDP daemon is restarted independently of OVS",
             lambda: json.loads(state.read_text()).get("pid") not in (None, old_pid)
             and json.loads(state.read_text())["state"] == "running", seconds=60)
    assert ping()
    wait_for("LLDP neighbors return after daemon restart", lambda: peers("s1") == expected["s1"], seconds=40)
    print(cli("s1", "show lldp neighbors").stdout, flush=True)
    # Per-port RX/TX controls, with the existing commit behavior (all ports
    # briefly go down). Wait for both discovery and forwarding to reconverge.
    cli("s1", "conf t", "int eth1", "no lldp transmit", "end")
    wait_for("receive-only port keeps learning but stops advertising",
             lambda: peers("s1") == expected["s1"] and peers("s2") == {("eth2", "s3", "eth2")}, seconds=65)
    assert "RX only" in cli("s1", "show lldp interface eth1").stdout
    cli("s1", "conf t", "int eth1", "lldp transmit", "no lldp receive", "end")
    wait_for("transmit-only port advertises but does not learn",
             lambda: peers("s1") == {("eth2", "s3", "eth1")} and peers("s2") == expected["s2"], seconds=65)
    cli("s1", "conf t", "int eth1", "no lldp transmit", "end")
    wait_for("both directions disabled exclude the port",
             lambda: peers("s1") == {("eth2", "s3", "eth1")} and peers("s2") == {("eth2", "s3", "eth2")}, seconds=65)
    assert "disabled" in cli("s1", "show lldp interface eth1").stdout
    cli("s1", "conf t", "no lldp run", "end")
    wait_for("global LLDP disable stops the discovery daemon",
             lambda: json.loads(state.read_text())["state"] == "disabled")
    assert "LLDP is disabled" in cli("s1", "show lldp neighbors").stdout
    wait_for("forwarding continues with LLDP disabled", ping)
    cli("s1", "conf t", "lldp run", "int eth1", "lldp transmit", "lldp receive", "end", "wr")
    wait_for("re-enabled LLDP restores neighbors", lambda: peers("s1") == expected["s1"], seconds=65)

    # Accelerate only this test's advertisements, then freeze the whole LLDP
    # process group so no shutdown PDU can remove a neighbor before TTL expiry.
    sock = "/run/ll2s/s1/lldp.sock"
    for cmd in (("configure", "lldp", "tx-interval", "1"),
                ("configure", "lldp", "tx-hold", "3"), ("update",)):
        ns("s1", "lldpcli", "-u", sock, *cmd)
    wait_for("peer receives short test TTL", lambda: any(p["name"] == "eth1" and
             p["port"][0]["ttl"][0]["value"] == "3" for p in records("s2")))
    pid = json.loads(state.read_text())["pid"]
    os.killpg(pid, signal.SIGSTOP)
    try:
        wait_for("silent neighbor expires by advertised TTL",
                 lambda: peers("s2") == {("eth2", "s3", "eth2")}, seconds=10)
        assert ping()
    finally:
        os.killpg(pid, signal.SIGCONT)
    wait_for("neighbor returns after advertisements resume", lambda: peers("s2") == expected["s2"])
