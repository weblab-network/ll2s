import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ll2s.backend import OVSBridge, PreflightError
from ll2s.cli import Shell
from ll2s.config import Config, ConfigError, Port
from ll2s.snmp import SnmpService, daemon_config, show


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.config = Config(management_address="192.0.2.10/24", snmp_community="lab-test")

    def test_cli_roundtrip_candidate_and_old_config(self):
        shell = Shell(Mock(), self.runtime, self.runtime / "startup", out=lambda _: None)
        for command in ("conf t", "vlan 100", "management vlan 100",
                        "management ip address 192.0.2.10/24", "ip default-gateway 192.0.2.1",
                        "snmp-server community lab-test ro", "snmp-server source 192.0.2.0/24",
                        'snmp-server contact "Lab Operator"', 'snmp-server location "Rack 1"'):
            shell.execute(command)
        self.assertFalse(shell.running.snmp_community)
        shell.execute("end")
        shell.execute("wr")
        shell.load(shell.startup.read_text())
        self.assertEqual(shell.running, shell.candidate)
        self.assertEqual(Config.from_dict(json.loads(json.dumps(shell.running.to_dict()))), shell.running)
        shell.execute("no snmp-server community")
        shell.execute("no management ip address")
        self.assertFalse(shell.candidate.management_gateway)
        shell.execute("abort")
        self.assertEqual(shell.candidate, shell.running)
        old = Config.from_dict({"ports": {"eth1": {}}})
        self.assertFalse(old.management_address or old.snmp_community)
        self.assertEqual(shell.startup.stat().st_mode & 0o777, 0o600)

    def test_invalid_configuration(self):
        for fields in (
            {"management_address": "0.0.0.0/24"}, {"management_address": "192.0.2.0/24"},
            {"management_address": "::1/128"}, {"management_address": "192.0.2.10"},
            {"management_address": ""}, {"management_vlan": 100},
            {"management_gateway": "198.51.100.1"}, {"management_gateway": "192.0.2.10"},
            {"snmp_community": "hello\nworld"}, {"snmp_community": '"quoted"'},
            {"snmp_source": "0.0.0.0/0\ngroup bad"}, {"snmp_contact": "bad\nconfig"},
            {"snmp_location": "bad#comment"}, {"ports": {"ll2s-mgmt": Port()}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigError):
                config = copy.deepcopy(self.config)
                for key, value in fields.items():
                    setattr(config, key, value)
                config.validate()

    def test_v2c_only_read_view_and_binding(self):
        text = daemon_config(self.config, self.runtime)
        self.assertIn("agentaddress udp:192.0.2.10:161", text)
        self.assertIn("group readonly v2c monitor", text)
        self.assertIn("exact monitoring none none", text)
        self.assertNotIn("rocommunity", text)
        self.assertNotIn("included .1\n", text)
        self.assertIn("agentXPerms 0600", text)

    def test_management_refuses_foreign_interface_and_route(self):
        bridge = OVSBridge(self.runtime)
        from ll2s.management import preflight
        with patch.object(bridge, "rows", return_value=[]):
            with self.assertRaises(PreflightError):
                preflight(bridge, self.config, {"ll2s-mgmt": {}}, {}, {})
        self.config.management_gateway = "192.0.2.1"
        with patch("ll2s.management.run", return_value='[{"dev":"foreign"}]'):
            with self.assertRaises(PreflightError):
                preflight(bridge, self.config, {}, {}, {})

    def test_monitoring_failure_retry_disable_and_secret_free_status(self):
        state = self.runtime / "running.json"
        state.write_text(json.dumps(self.config.to_dict()))
        service = SnmpService(self.runtime)
        with patch("ll2s.snmp.subprocess.Popen", side_effect=OSError("missing")) as start:
            service.tick()
            service.next_poll = 0
            service.tick()
            self.assertEqual(start.call_count, 1)
            self.assertIn("unavailable", show(self.runtime, self.config))
            self.assertNotIn(self.config.snmp_community, show(self.runtime, self.config))
        process = Mock(pid=123)
        process.poll.return_value = None
        service.retry = service.next_poll = 0
        with patch("ll2s.snmp.subprocess.Popen", return_value=process):
            service.tick()
        self.assertIn("starting", show(self.runtime, self.config))
        self.assertEqual((self.runtime / "snmpd.conf").stat().st_mode & 0o777, 0o600)
        (self.runtime / "agentx.sock").touch()
        service.next_poll = 0
        service.tick()
        self.assertIn("running", show(self.runtime, self.config))
        (self.runtime / "FAILED").touch()
        service.next_poll = 0
        service.tick()
        process.terminate.assert_called_once()
        self.assertFalse((self.runtime / "snmpd.conf").exists())
