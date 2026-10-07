"""Opt-in LLDP/management/SNMP check through the sibling Weblab's actual TAP/macvlan fabric.

Requires root, Docker, Weblab's native dependencies, and ll2s:dev. Uses only a
disposable Lab directory and reserved node IDs 920-922. Run node suites serially.
Set WEBLAB_SOURCE to the Weblab checkout and LL2S_WEBLAB_TEST=1 explicitly.
"""

import json
import os
import re
from pathlib import Path
import sys
import tempfile
import time


def main():
    if os.environ.get("LL2S_WEBLAB_TEST") != "1":
        raise SystemExit("Set LL2S_WEBLAB_TEST=1 and WEBLAB_SOURCE; requires disposable native-test resources.")
    sys.path.insert(0, os.environ["WEBLAB_SOURCE"])
    import lab_server
    import console_capture

    def wait(predicate, label, timeout=65):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                print("PASS: " + label, flush=True)
                return
            time.sleep(0.5)
        raise AssertionError(label)

    with tempfile.TemporaryDirectory(prefix="ll2s-weblab-lldp-") as tmp:
        lab = lab_server.Lab(Path(tmp) / "data", Path(tmp) / "images")
        consoles = {}
        nodes = []
        for i in (1, 2, 3):
            config = f"hostname SW{i}\nspanning-tree mode rstp\nspanning-tree priority {4096*i}\nlldp run\n"
            config += (f"management ip address 10.222.0.{i}/24\n"
                       "snmp-server community lab-test ro\n")
            for port in (0, 1):
                config += f"interface eth{port}\nexit\n"
            nodes.append({"id": f"s{i}", "name": f"SW{i}", "type": "switch", "image": "ll2s:dev",
                          "ethernet": 2, "iol_id": 919 + i, "startup_config": config, "x": i * 200, "y": 200})
        links = [{"id": "s12", "a": {"node": "s1", "port": "eth0"}, "b": {"node": "s2", "port": "eth0"}},
                 {"id": "s23", "a": {"node": "s2", "port": "eth1"}, "b": {"node": "s3", "port": "eth0"}},
                 {"id": "s13", "a": {"node": "s1", "port": "eth1"}, "b": {"node": "s3", "port": "eth1"}}]

        def command(node, *args):
            return lab_server.run("docker", "exec", lab.runtime[node]["container"], *args)

        def cli(node, line):
            if node not in consoles:
                consoles[node] = console_capture.Console(lab.runtime[node]["port"])
                consoles[node].acquire()
                cli(node, "")
            console = consoles[node]
            console.send(line.encode() + b"\r")
            output = b""
            deadline = time.monotonic() + 15
            while not re.search(rb"(?m)^[^\r\n]*# ", output):
                output += console.receive(deadline)
            assert b"% " not in output, output
            return output.decode(errors="replace")

        def close_consoles():
            for console in consoles.values():
                console.close()
            consoles.clear()

        expected = {"s1": {("eth0", "SW2", "eth0"), ("eth1", "SW3", "eth1")},
                    "s2": {("eth0", "SW1", "eth0"), ("eth1", "SW3", "eth0")},
                    "s3": {("eth0", "SW2", "eth1"), ("eth1", "SW1", "eth1")}}

        def discovered():
            for node in expected:
                try:
                    doc = json.loads(command(node, "lldpcli", "-u", "/run/ll2s/lldp.sock",
                                             "-f", "json0", "show", "neighbors", "details"))
                except lab_server.LabError:
                    return False
                peers = {(p["name"], p["chassis"][0]["name"][0]["value"], p["port"][0]["id"][0]["value"])
                         for g in doc["lldp"] for p in g.get("interface", [])}
                if peers != expected[node]:
                    return False
            return True

        def poll():
            try:
                return "SW1" in command("s2", "snmpget", "-v2c", "-c", "lab-test", "-t", "1", "-r", "0",
                                        "-Oqv", "10.222.0.1", ".1.3.6.1.2.1.1.5.0")
            except lab_server.LabError:
                return False

        def counter():
            link = json.loads(command("s1", "ip", "-j", "link", "show", "eth0"))[0]
            return int(command("s2", "snmpget", "-v2c", "-c", "lab-test", "-t", "1", "-r", "0",
                               "-Oqv", "10.222.0.1", f".1.3.6.1.2.1.31.1.1.1.6.{link['ifindex']}"))

        try:
            lab.save({"name": "Disposable LLDP triangle", "nodes": nodes, "links": links})
            lab.start_all()
            wait(lambda: "Alternate" in command("s3", "ovs-vsctl", "--db=unix:/run/ll2s/ovs/db.sock",
                                               "get", "Port", "eth0", "rstp_status"),
                 "Weblab triangle has an RSTP-discarding port")
            wait(discovered, "LLDP peers and port IDs correct through Weblab TAP/macvlan, including blocked link")
            wait(poll, "SNMPv2c polling through Weblab TAP/macvlan management VLAN")
            before_counter = counter()
            command("s2", "ping", "-c", "5", "-s", "1000", "10.222.0.1")
            wait(lambda: counter() > before_counter, "macvlan ifHCInOctets increases with real traffic")
            for node in expected:
                print(cli(node, "show lldp neighbors"), flush=True)
                assert "running" in cli(node, "show snmp")
                cli(node, "wr")
            close_consoles()
            lab.stop_all()
            lab.start_all()
            wait(poll, "Weblab saved Stop/Start restores management and SNMP community")
            wait(discovered, "Weblab saved Stop/Start restores LLDP discovery")
        finally:
            close_consoles()
            lab.stop_all()
            lab.file_lock.close()
            assert not lab_server.run("docker", "ps", "-aq", "--filter", f"label=iol.lab={lab.owner}")
            assert not lab_server.run("docker", "network", "ls", "-q", "--filter", f"label=iol.lab={lab.owner}")
            print("Disposable Weblab LLDP resources cleaned", flush=True)


if __name__ == "__main__":
    main()
