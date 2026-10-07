"""Exercise real keystrokes through a controlling PTY with a mocked backend.

Actual OVS commits are covered separately by capabilities.py.
"""

import fcntl
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        import readline
        if "libedit" in (readline.__doc__ or ""):
            self.skipTest("GNU Readline required for immediate help")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.master, slave = pty.openpty()
        self.original_terminal = termios.tcgetattr(slave)

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        script = ("from unittest.mock import patch\n"
                  "from ll2s.cli import main\n"
                  "with patch('ll2s.cli.OVSBridge'):\n"
                  "    raise SystemExit(main())\n")
        self.child = subprocess.Popen(
            [sys.executable, "-c", script, "--runtime-dir", self.temp.name,
             "--startup", str(Path(self.temp.name)/"startup.conf")],
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=controlling_terminal,
            env={**os.environ, "TERM": "xterm", "INPUTRC": "/dev/null"})
        os.close(slave)
        self.addCleanup(self.close)
        output = self.until(b"ll2s# ")
        self.assertIn(b"no Enter needed", output)

    def close(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=5)
        os.close(self.master)

    def until(self, marker):
        output = b""
        deadline = time.monotonic()+5
        while marker not in output:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([self.master], [], [], remaining)[0]:
                self.fail(f"No {marker!r}; received {output!r}")
            try:
                output += os.read(self.master, 65536)
            except OSError:
                self.fail(f"CLI exited ({self.child.poll()}): {output!r}")
        return output

    def send(self, text, marker):
        os.write(self.master, text)
        return self.until(marker)

    def test_help_without_enter_preserves_line_cursor_and_history(self):
        output = self.send(b"?", b"ll2s# ")
        self.assertIn(b"show version", output)
        self.assertNotIn(b"unknown command", output)
        output = self.send(b"show ?", b"ll2s# show ")
        self.assertIn(b"show version", output)
        self.assertNotIn(b"configure terminal", output)
        output = self.send(b"version\r", b"ll2s# ")
        self.assertIn(b"Linux Layer 2 Switch", output)
        # History contains the submitted command, not the help keystroke.
        output = self.send(b"\x1b[A\r", b"ll2s# ")
        self.assertIn(b"Linux Layer 2 Switch", output)
        # Help in the middle of a line leaves the insertion point unchanged.
        output = self.send(b"show versin\x1b[D?", b"ll2s# show versin")
        self.assertIn(b"show version", output)
        output = self.send(b"o\r", b"ll2s# ")
        self.assertIn(b"Linux Layer 2 Switch", output)
        output = self.send(b"nonsense?", b"ll2s# nonsense")
        self.assertIn(b"No matching commands", output)
        self.send(b"\x15", b"\r")  # Ctrl+U clears the pending line.
        self.send(b"exit\r", b"exit")
        self.assertEqual(self.child.wait(timeout=5), 0)

    def test_mode_help_completion_and_candidate_editing(self):
        self.send(b"conf t\r", b"ll2s(config)# ")
        output = self.send(b"?", b"ll2s(config)# ")
        self.assertIn(b"interface <name>", output)
        self.send(b"interface eth3\r", b"ll2s(config-if)# ")
        output = self.send(b"switchport ?", b"ll2s(config-if)# switchport ")
        self.assertIn(b"switchport mode <access|trunk>", output)
        self.send(b"mode trunk\r", b"ll2s(config-if)# ")
        output = self.send(b"show candidate-config\r", b"ll2s(config-if)# ")
        self.assertIn(b"interface eth3", output)
        self.assertIn(b"switchport mode trunk", output)
        self.send(b"abort\r", b"ll2s(config)# ")
        self.send(b"end\r", b"ll2s# ")
        output = self.send(b"show ver\t\r", b"ll2s# ")
        self.assertIn(b"Linux Layer 2 Switch", output)

    def test_abbreviated_help_tab_and_vlan_names(self):
        output = self.send(b"sh ru?", b"ll2s# sh ru")
        self.assertIn(b"show running-config", output)
        self.assertNotIn(b"No matching commands", output)
        output = self.send(b"nn\t\r", b"ll2s# ")
        self.assertIn(b"hostname ll2s", output)
        self.assertNotIn(b"unknown command", output)
        self.send(b"conf t\r", b"ll2s(config)# ")
        self.send(b"vlan 100 name USERS\r", b"ll2s(config)# ")
        output = self.send(b"do sh cand\t\r", b"ll2s(config)# ")
        self.assertIn(b"vlan 100 name USERS", output)

    def test_bare_write_help_and_completion(self):
        output = self.send(b"wr?", b"ll2s# wr")
        self.assertIn(b"<cr>", output)
        output = self.send(b"\r", b"ll2s# ")
        self.assertIn(b"Startup configuration saved.", output)
        output = self.send(b"wr\t\r", b"ll2s# ")
        self.assertIn(b"Startup configuration saved.", output)
        self.assertTrue((Path(self.temp.name) / "startup.conf").exists())

    def test_control_z_commits_and_discards_unsubmitted_line(self):
        self.send(b"conf t\r", b"ll2s(config)# ")
        self.send(b"hostname SW3\r", b"SW3(config)# ")
        output = self.send(b"hostname UNFINISHED\x1a", b"SW3# ")
        self.assertIn(b"Commit complete", output)
        output = self.send(b"sh run\r", b"SW3# ")
        self.assertIn(b"hostname SW3", output)
        self.assertNotIn(b"UNFINISHED", output)
        self.assertFalse((Path(self.temp.name) / "startup.conf").exists())
        self.send(b"conf t\r", b"SW3(config)# ")
        self.send(b"int eth1\r", b"SW3(config-if)# ")
        output = self.send(b"\x1a", b"SW3# ")
        self.assertIn(b"Commit complete", output)
        self.send(b"conf t\r", b"SW3(config)# ")
        output = self.send(b"\x1a", b"SW3# ")
        self.assertNotIn(b"Commit complete", output)
        # At exec, Ctrl+Z neither suspends nor submits a pending command.
        output = self.send(b"sh ver\x1asion\r", b"SW3# ")
        self.assertIn(b"Linux Layer 2 Switch", output)
        self.send(b"exit\r", b"exit")
        self.assertEqual(self.child.wait(timeout=5), 0)
        self.assertEqual(termios.tcgetattr(self.master), self.original_terminal)

    def test_control_z_failed_commit_preserves_config_mode(self):
        self.send(b"conf t\r", b"ll2s(config)# ")
        self.send(b"int eth1\r", b"ll2s(config-if)# ")
        self.send(b"sw access vlan 999\r", b"ll2s(config-if)# ")
        output = self.send(b"\x1a", b"ll2s(config-if)# ")
        self.assertIn(b"declare referenced VLANs", output)
        output = self.send(b"do sh cand\r", b"ll2s(config-if)# ")
        self.assertIn(b"switchport access vlan 999", output)
        self.send(b"abort\r", b"ll2s(config)# ")
        self.send(b"\x1a", b"ll2s# ")


if __name__ == "__main__":
    unittest.main()
