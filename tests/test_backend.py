import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ll2s.backend import BackendError, OVSBridge, decode
from ll2s.config import Config, Port


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = OVSBridge(self.tmp.name)

    def test_refuses_existing_unowned_bridge_before_mutation(self):
        with patch.object(self.bridge, "appctl"), \
             patch.object(self.bridge, "topology", return_value=({"br0": {"external_ids": {}}}, {}, {})), \
             patch.object(self.bridge, "links", return_value={}), patch("ll2s.backend.run") as run:
            with self.assertRaisesRegex(BackendError, "not an LL2S OVS bridge"):
                self.bridge.preflight(Config())
        run.assert_not_called()

    def test_refuses_existing_linux_bridge(self):
        with patch.object(self.bridge, "appctl"), \
             patch.object(self.bridge, "topology", return_value=({}, {}, {})), \
             patch.object(self.bridge, "links", return_value={"br0": {}}):
            with self.assertRaisesRegex(BackendError, "non-OVS"):
                self.bridge.preflight(Config())

    def test_refuses_port_owned_by_another_master(self):
        with patch.object(self.bridge, "appctl"), \
             patch.object(self.bridge, "topology", return_value=({}, {}, {})), \
             patch.object(self.bridge, "links", return_value={"eth1": {"master": "other", "link_type": "ether"}}), \
             patch("ll2s.backend.run") as run:
            with self.assertRaisesRegex(BackendError, "another bridge/bond"):
                self.bridge.preflight(Config(ports={"eth1": Port()}))
        run.assert_not_called()

    def test_refuses_addressed_port(self):
        with patch.object(self.bridge, "appctl"), \
             patch.object(self.bridge, "topology", return_value=({}, {}, {})), \
             patch.object(self.bridge, "links", return_value={"eth1": {"link_type": "ether"}}), \
             patch("ll2s.backend.run", return_value=json.dumps([{"addr_info": [{"scope": "global"}]}])) as run:
            with self.assertRaisesRegex(BackendError, "has an IP address"):
                self.bridge.preflight(Config(ports={"eth1": Port()}))
        self.assertEqual(run.call_count, 1)
        self.assertIn("show", run.call_args.args)

    def test_failed_ovs_attachment_never_enables_ports(self):
        with patch.object(self.bridge, "preflight", return_value=({}, set())), \
             patch.object(self.bridge, "remember_ports"), patch.object(self.bridge, "stop_forwarding"), \
             patch.object(self.bridge, "vsctl"), \
             patch.object(self.bridge, "rows", return_value=[{"name": "eth1", "ofport": -1, "error": "failure"}]), \
             patch("ll2s.backend.run") as run:
            with self.assertRaisesRegex(BackendError, "could not attach"):
                self.bridge.apply(Config(protocol="rstp", ports={"eth1": Port()}), Config())
        self.assertFalse(any("up" in call.args for call in run.call_args_list))

    def test_refuses_unmanaged_membership_before_stopping_ports(self):
        with patch.object(self.bridge, "preflight", return_value=({}, {"eth9"})), \
             patch.object(self.bridge, "stop_forwarding") as stop:
            with self.assertRaisesRegex(BackendError, "membership differs"):
                self.bridge.apply(Config(), Config())
        stop.assert_not_called()

    def test_unsupported_bpduguard_is_not_silently_accepted(self):
        with patch.object(self.bridge, "vsctl") as vsctl:
            with self.assertRaisesRegex(BackendError, "BPDU guard is not supported"):
                self.bridge.preflight(Config(protocol="rstp", ports={"eth1": Port(bpduguard=True)}))
        vsctl.assert_not_called()

    def test_fail_closed_journal_does_not_touch_replacement_interface(self):
        Path(self.tmp.name, "active-ports.json").write_text(json.dumps({"eth1": [4, "aa:bb"], "eth2": [5, "cc:dd"]}))
        with patch.object(self.bridge, "links", return_value={
            "eth1": {"ifindex": 4, "address": "aa:bb"},
            "eth2": {"ifindex": 6, "address": "ee:ff"},
        }), patch("ll2s.backend.run") as run:
            self.bridge.stop_forwarding()
        run.assert_called_once_with("ip", "link", "set", "dev", "eth1", "down")

    def test_ovsdb_encodings(self):
        self.assertEqual(decode(["map", [["owner", "ll2s"]]]), {"owner": "ll2s"})
        self.assertEqual(decode(["set", [["uuid", "first"], ["uuid", "second"]]]), ["first", "second"])
        self.assertEqual(decode(["uuid", "one"]), "one")
        self.assertEqual(decode(["set", []]), [])
        self.assertEqual(decode(False), False)


if __name__ == "__main__":
    unittest.main()
