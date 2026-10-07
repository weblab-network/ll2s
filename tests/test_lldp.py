import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ll2s.backend import BackendError
from ll2s.cli import Shell
from ll2s.config import Config, ConfigError, Port
from ll2s.lldp import DiscoveryService, fingerprint, policy, show


class LldpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.config = Config(hostname="SW1", lldp=True, ports={"eth1": Port()})

    def ready(self):
        (self.runtime / "lldp-status.json").write_text(json.dumps({
            "state": "running", "policy": fingerprint(policy(self.config))}))

    def test_config_roundtrip_legacy_and_validation(self):
        shell = Shell(Mock(), self.runtime, self.runtime / "startup", out=lambda _: None)
        for command in ("conf t", "lldp run", "int eth1", "no lldp transmit", "exit", "commit", "wr"):
            shell.execute(command)
        self.assertTrue(shell.running.lldp)
        self.assertFalse(shell.running.ports["eth1"].lldp_transmit)
        shell.load(shell.startup.read_text())
        self.assertEqual(shell.candidate, shell.running)
        self.assertEqual(Config.from_dict(json.loads(json.dumps(shell.running.to_dict()))), shell.running)
        shell.execute("no lldp run")
        shell.execute("abort")
        self.assertTrue(shell.candidate.lldp)
        old = Config.from_dict({"ports": {"eth1": {}}})
        self.assertFalse(old.lldp)
        self.assertTrue(old.ports["eth1"].lldp_receive)
        with self.assertRaises(ConfigError):
            Config(lldp="yes").validate()
        with self.assertRaises(ConfigError):
            Config(ports={"eth1": Port(lldp_transmit=1)}).validate()

    def test_unavailable_pending_disabled_and_interface_filter(self):
        self.assertIn("unavailable", show(self.runtime, "lldp neighbors", self.config))
        self.ready()
        with patch("ll2s.lldp.query", side_effect=BackendError("socket gone")):
            self.assertIn("socket gone", show(self.runtime, "lldp neighbors", self.config))
        self.config.hostname = "SW2"
        self.assertIn("pending", show(self.runtime, "lldp neighbors", self.config))
        self.config.lldp = False
        self.assertIn("disabled", show(self.runtime, "lldp neighbors", self.config))
        with self.assertRaises(ConfigError):
            show(self.runtime, "lldp neighbors interface bad", self.config)

    def test_neighbor_formatting_optional_fields_and_terminal_safety(self):
        self.ready()
        records = [{"name": "eth1", "chassis": [{"name": [{"value": "SW2\x1b[31m\n"}],
                    "capability": [{"type": "Bridge", "enabled": True}]}],
                    "port": [{"id": [{"value": "eth2"}]}]}, {"name": "foreign"}]
        with patch("ll2s.lldp.query", return_value=records):
            output = show(self.runtime, "lldp neighbors", self.config)
            self.assertIn("Bridge", output)
            self.assertIn("eth2", output)
            self.assertNotIn("\x1b", output)
            self.assertNotIn("foreign", output)
            details = show(self.runtime, "lldp neighbors interface eth1 detail", self.config)
            self.assertIn("Chassis ID: -", details)
            self.assertIn("Management addresses: -", details)
        with patch("ll2s.lldp.query", return_value=[]):
            self.assertEqual(show(self.runtime, "lldp neighbors", self.config), "No LLDP neighbors.")

    def test_service_failure_isolated_and_retry_throttled(self):
        (self.runtime / "running.json").write_text(json.dumps(self.config.to_dict()))
        service = DiscoveryService(self.runtime)
        with patch("ll2s.lldp.subprocess.Popen", side_effect=OSError("not installed")) as start:
            service.tick()
            self.assertEqual(start.call_count, 1)
            self.assertEqual(json.loads((self.runtime / "lldp-status.json").read_text())["state"], "unavailable")
            service.next_poll = 0
            service.tick()
            self.assertEqual(start.call_count, 1)

    def test_daemon_starts_excluded_then_installs_port_policy(self):
        self.config.ports.update({"eth2": Port(lldp_transmit=False),
                                  "eth3": Port(lldp_receive=False), "eth4": Port(shutdown=True)})
        (self.runtime / "running.json").write_text(json.dumps(self.config.to_dict()))
        service = DiscoveryService(self.runtime)
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch("ll2s.lldp.subprocess.Popen", return_value=process) as start, \
                patch("ll2s.lldp.query", return_value=[{"name": n} for n in ("eth1", "eth2", "eth3")]), \
                patch("ll2s.lldp.client") as client:
            service.tick()
            self.assertIn("!*", start.call_args.args[0])
            service.next_poll = 0
            service.tick()
            self.assertEqual(service.phase, "running")
            client.assert_any_call(self.runtime, "configure", "ports", "eth2", "lldp", "status", "rx-only")
            client.assert_any_call(self.runtime, "configure", "ports", "eth3", "lldp", "status", "tx-only")
            client.assert_any_call(self.runtime, "configure", "system", "interface", "pattern", "!*,!!eth1,!!eth2,!!eth3")
            self.config.lldp = False
            (self.runtime / "running.json").write_text(json.dumps(self.config.to_dict()))
            service.next_poll = 0
            service.tick()
            process.terminate.assert_called_once()
            self.assertEqual(service.phase, "disabled")


if __name__ == "__main__":
    unittest.main()
