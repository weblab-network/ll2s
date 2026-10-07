import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ll2s.backend import BackendError, PreflightError, OVSBridge
from ll2s.cli import Shell
from ll2s.commands import suggestions
from ll2s.config import Config, ConfigError, Port, vlan_list


class FakeBackend:
    def __init__(self):
        self.applied = []
        self.failures = 0
        self.stopped = False

    def preflight(self, config):
        config.validate()

    def apply(self, config, previous):
        self.applied.append(copy.deepcopy(config))
        if self.failures:
            self.failures -= 1
            raise BackendError("injected netlink failure")

    def stop_forwarding(self):
        self.stopped = True


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = FakeBackend()
        self.output = []
        self.shell = Shell(self.backend, self.tmp.name, Path(self.tmp.name) / "startup.conf", self.output.append)

    def commands(self, *commands):
        for command in commands:
            self.shell.execute(command)

    def test_candidate_commit_and_save_restore(self):
        self.commands("conf t", "hostname SW1", "vlan 10,20", "interface eth1",
                      "switchport mode trunk", "switchport trunk allowed vlan 10,20", "exit")
        self.assertEqual(self.shell.running.hostname, "ll2s")
        self.assertFalse(self.backend.applied)
        with self.assertRaises(ConfigError):
            self.shell.execute("write memory")
        self.commands("commit", "end", "write memory")
        self.assertEqual(self.shell.running.hostname, "SW1")
        restarted = Shell(self.backend, self.tmp.name, self.shell.startup, self.output.append)
        self.assertEqual(restarted.running, self.shell.running)
        restarted.load(self.shell.startup.read_text())
        self.assertEqual(restarted.candidate, self.shell.running)

    def test_invalid_vlan_reference_never_applies(self):
        self.commands("conf t", "interface eth1", "switchport access vlan 999", "exit")
        with self.assertRaises(ConfigError):
            self.shell.commit()
        self.assertEqual(self.backend.applied, [])

    def test_bare_write_saves_and_preserves_save_guards(self):
        for command in ("wr", "write", "wr mem", "write memory"):
            with self.subTest(command=command):
                self.shell.execute(command)
                self.assertEqual(self.shell.startup.read_text(), self.shell.running.render())
                self.assertEqual(self.output[-1], "Startup configuration saved.")
        self.commands("wr?")
        self.assertIn("<cr>", self.output[-1])
        self.commands("conf t", "hostname Pending")
        saved = self.shell.startup.read_text()
        for command in ("wr", "write"):
            with self.subTest(command=command), self.assertRaisesRegex(ConfigError, "uncommitted"):
                self.shell.execute(command)
        self.assertEqual(self.shell.startup.read_text(), saved)
        self.commands("abort")
        self.shell.healthy = False
        for command in ("wr", "write"):
            with self.subTest(command=command), self.assertRaisesRegex(ConfigError, "uncertain"):
                self.shell.execute(command)
        self.assertEqual(self.shell.startup.read_text(), saved)

    def test_rollback_preserves_running_and_startup(self):
        self.commands("write memory", "conf t", "hostname Changed")
        original = self.shell.startup.read_text()
        self.backend.failures = 1
        with self.assertRaisesRegex(BackendError, "previous configuration restored"):
            self.shell.commit()
        self.assertEqual(self.shell.running.hostname, "ll2s")
        self.assertEqual(self.backend.applied[-1].hostname, "ll2s")
        self.assertEqual(self.shell.startup.read_text(), original)

    def test_preflight_failure_inside_apply_never_rolls_back_or_stops_forwarding(self):
        with patch.object(self.backend, "apply", side_effect=PreflightError("unmanaged port")) as apply:
            with self.assertRaisesRegex(PreflightError, "unmanaged port"):
                self.shell.commit()
        self.assertEqual(apply.call_count, 1)
        self.assertFalse(self.backend.stopped)
        self.assertTrue(self.shell.healthy)

    def test_failed_rollback_blocks_save_across_sessions(self):
        self.commands("conf t", "hostname Changed")
        self.backend.failures = 2
        with self.assertRaisesRegex(BackendError, "rollback failed"):
            self.shell.commit()
        self.assertTrue(self.backend.stopped)
        restarted = Shell(self.backend, self.tmp.name, self.shell.startup, self.output.append)
        with self.assertRaisesRegex(ConfigError, "uncertain"):
            restarted.execute("write memory")

    def test_config_files_cannot_execute_operational_commands(self):
        for line in ("commit", "write memory", "show interfaces", "configure terminal"):
            with self.subTest(line=line), self.assertRaises(ConfigError):
                self.shell.load("hostname okay\n" + line)
        self.assertFalse(self.backend.applied)

    def test_abort_and_modes(self):
        self.commands("conf t", "interface eth1", "shutdown", "abort")
        self.assertEqual(self.shell.mode, "config")
        self.assertFalse(self.shell.candidate.ports)
        self.commands("end")
        with self.assertRaises(ConfigError):
            self.shell.execute("hostname nope")

    def test_end_commits_pending_changes_without_saving_startup(self):
        self.commands("wr", "conf t", "hostname SW1", "end")
        self.assertEqual(self.shell.mode, "exec")
        self.assertEqual(self.shell.running.hostname, "SW1")
        self.assertIn("hostname ll2s", self.shell.startup.read_text())
        self.assertEqual(len(self.backend.applied), 1)
        self.commands("conf t", "interface eth1", "end")
        self.assertEqual(self.shell.mode, "exec")
        self.assertIsNone(self.shell.interface)
        self.assertIn("eth1", self.shell.running.ports)
        self.assertEqual(len(self.backend.applied), 2)
        self.commands("conf t", "end", "end")
        self.assertEqual(len(self.backend.applied), 2)
        self.commands("wr")
        self.assertEqual(self.shell.startup.read_text(), self.shell.running.render())

    def test_failed_end_stays_in_interface_mode_with_candidate(self):
        self.commands("conf t", "interface eth1", "switchport access vlan 999")
        with self.assertRaises(ConfigError):
            self.shell.execute("end")
        self.assertEqual(self.shell.mode, "interface")
        self.assertEqual(self.shell.interface, "eth1")
        self.assertEqual(self.shell.candidate.ports["eth1"].access, 999)
        self.assertFalse(self.backend.applied)
        self.commands("switchport access vlan 1")
        self.backend.failures = 1
        with self.assertRaisesRegex(BackendError, "previous configuration restored"):
            self.shell.execute("end")
        self.assertEqual(self.shell.mode, "interface")
        self.assertEqual(self.shell.interface, "eth1")
        self.assertNotIn("eth1", self.shell.running.ports)
        self.assertIn("eth1", self.shell.candidate.ports)
        self.commands("end")
        self.assertEqual(self.shell.mode, "exec")
        self.assertIn("eth1", self.shell.running.ports)

    def test_end_retries_uncertain_state_and_file_end_is_only_syntax(self):
        self.shell.healthy = False
        self.commands("conf t", "end")
        self.assertTrue(self.shell.healthy)
        self.assertEqual(len(self.backend.applied), 1)
        self.shell.load("hostname FromFile\nend\n")
        self.assertEqual(len(self.backend.applied), 1)
        self.assertEqual(self.shell.running.hostname, "ll2s")

    def test_invalid_input_is_rejected(self):
        self.commands("conf t")
        for command in ("spanning-tree priority 1", "spanning-tree mode mstp", "interface ../evil",
                        "interface lo", "interface br0", "vlan 4095", "vlan 30-20", "hostname 'a;b'"):
            with self.subTest(command=command), self.assertRaises(ConfigError):
                self.shell.execute(command)

    def test_rstp_configuration_roundtrip(self):
        c = Config(protocol="rstp", vlans=[1, 20], ports={"eth1": Port(access=20, edge=True, bpduguard=True)})
        self.shell.load(c.render())
        self.assertEqual(self.shell.candidate, c)
        self.shell.candidate.protocol = "stp"
        with self.assertRaisesRegex(ConfigError, "require rstp"):
            self.shell.candidate.validate()

    def test_ranges(self):
        self.assertEqual(vlan_list("10,20-22,10"), [10, 20, 21, 22])
        for value in ("", "1-2-3", "0", "4095", "-1"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                vlan_list(value)

    def test_keyword_abbreviations_share_help_and_completion(self):
        self.commands("sh ru?")
        self.assertIn("show running-config", self.output[-1])
        self.assertNotIn("show startup-config", self.output[-1])
        self.assertEqual(suggestions(self.shell.commands(), "sh runn", completion=True),
                         ["show running-config"])
        self.commands("sh runn")
        self.assertIn("hostname ll2s", self.output[-1])
        self.commands("conf t", "host SW2", "int eth3", "sw mo tr", "do sh cand")
        self.assertIn("switchport mode trunk", self.output[-1])
        with self.assertRaisesRegex(ConfigError, "ambiguous keyword"):
            self.shell.execute("sh s")
        with self.assertRaisesRegex(ConfigError, "ambiguous keyword"):
            self.shell.execute("s")
        with self.assertRaisesRegex(ConfigError, "incomplete"):
            self.shell.execute("sw mo")
        self.commands("sw mo access")
        self.assertEqual(self.shell.candidate.ports["eth3"].mode, "access")

    def test_vlan_names_save_restore_abort_and_delete(self):
        self.commands("conf t", "vlan 100 name USERS", "vlan 200 name \"Guest's LAN\"")
        self.assertEqual(self.shell.running.vlan_names, {})
        self.commands("commit", "end", "wr mem")
        restarted = Shell(self.backend, self.tmp.name, self.shell.startup, self.output.append)
        self.assertEqual(restarted.running.vlan_names, {100: "USERS", 200: "Guest's LAN"})
        restarted.load(self.shell.startup.read_text())
        self.assertEqual(restarted.candidate, self.shell.running)
        with patch.object(OVSBridge, "vsctl", return_value="live port data"):
            table = OVSBridge(self.tmp.name).show("vlan", restarted.running)
        self.assertIn("USERS", table)
        self.assertIn("Guest's LAN", table)
        self.assertIn("live port data", table)
        self.commands("conf t", "vlan 100 name STAFF", "abort")
        self.assertEqual(self.shell.candidate.vlan_names[100], "USERS")
        self.commands("no vlan 100 name")
        self.assertIn(100, self.shell.candidate.vlans)
        self.assertNotIn(100, self.shell.candidate.vlan_names)
        self.commands("no vlan 200", "vlan 200")
        self.assertNotIn(200, self.shell.candidate.vlan_names)

    def test_vlan_name_validation_and_legacy_state(self):
        self.assertEqual(Config.from_dict({"vlans": [1, 10]}).vlan_names, {})
        self.commands("conf t")
        for value in ("''", "'   '", "'" + "x" * 33 + "'", "'bad\nname'"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "VLAN name"):
                self.shell.execute("vlan 100 name " + value)
        self.assertNotIn(100, self.shell.candidate.vlans)
        with self.assertRaises(ConfigError):
            self.shell.execute("vlan 100,200 name USERS")
        with self.assertRaisesRegex(ConfigError, "undeclared"):
            Config(vlan_names={100: "USERS"}).validate()


if __name__ == "__main__":
    unittest.main()
