"""Readline editing with immediate, non-destructive context help on Linux."""

import ctypes
import os
import sys
import termios

from .commands import suggestions


class LineEditor:
    """Keep the C key callback alive for the lifetime of the interactive shell.

    Python exposes completion but not custom Readline commands. Bind '?' through
    the public GNU Readline API already used by Python in the LL2S image. Loading
    the extension itself resolves its actual library, rather than guessing a
    second libreadline version. Non-GNU builds retain Enter-based help.
    """

    def __init__(self, shell):
        import readline

        self.shell = shell
        self.readline = readline
        readline.set_completer_delims("\n")
        readline.set_completer(self.complete)
        readline.parse_and_bind("tab: complete")
        self.immediate_help = False
        self.control_z = False
        if "libedit" in (readline.__doc__ or ""):
            return
        try:
            self.library = ctypes.CDLL(readline.__file__)
            self.point = ctypes.c_int.in_dll(self.library, "rl_point")
            self.buffer = ctypes.c_char_p.in_dll(self.library, "rl_line_buffer")
            callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_int)
            self.callback = callback_type(self.show_help)
            self.library.rl_bind_key.argtypes = [ctypes.c_int, callback_type]
            self.library.rl_bind_key.restype = ctypes.c_int
            self.library.rl_on_new_line.argtypes = []
            self.library.rl_on_new_line.restype = ctypes.c_int
            self.library.rl_crlf.argtypes = []
            self.library.rl_crlf.restype = ctypes.c_int
            self.immediate_help = self.library.rl_bind_key(ord("?"), self.callback) == 0
            self.library.rl_replace_line.argtypes = [ctypes.c_char_p, ctypes.c_int]
            self.library.rl_replace_line.restype = None
            self.library.rl_newline.argtypes = [ctypes.c_int, ctypes.c_int]
            self.library.rl_newline.restype = ctypes.c_int
            self.end_callback = callback_type(self.end_configuration)
            self.control_z = self.library.rl_bind_key(26, self.end_callback) == 0
        except (AttributeError, OSError, ValueError):
            # Tab/editing still work; the ordinary command parser handles '?'.
            pass

    def read(self, prompt):
        if not self.control_z or not sys.stdin.isatty():
            return input(prompt)
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        editing = termios.tcgetattr(fd)
        # Deliver Ctrl+Z to Readline instead of letting the terminal suspend us.
        editing[6][termios.VSUSP] = bytes([os.fpathconf(fd, "PC_VDISABLE")])
        try:
            termios.tcsetattr(fd, termios.TCSANOW, editing)
            return input(prompt)
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, original)

    def end_configuration(self, count, key):
        if self.shell.mode == "exec":
            return 0
        try:
            # Submit through the normal input loop: commits and their errors must
            # run outside this C callback. Discard any unsubmitted partial line.
            self.library.rl_replace_line(b"end", 1)
            self.point.value = 3
            self.readline.redisplay()
            return self.library.rl_newline(1, key)
        except Exception as error:
            print(f"% Cannot end configuration: {error}", flush=True)
            return 0

    def complete(self, text, state):
        choices = suggestions(self.shell.commands(), text, completion=True)
        return choices[state] if state < len(choices) else None

    def show_help(self, count, key):
        # rl_point counts bytes, not Unicode characters. Neither the line buffer
        # nor the cursor is modified: typing and editing resume at the same spot.
        try:
            prefix = (self.buffer.value or b"")[:self.point.value].decode(
                sys.stdin.encoding or "utf-8", errors="replace")
            self.library.rl_crlf()
            self.shell.help(prefix)
            sys.stdout.flush()
        except Exception as error:
            # Never let a Python exception cross the C callback boundary.
            print(f"% Help unavailable: {error}", flush=True)
        finally:
            self.library.rl_on_new_line()
            self.readline.redisplay()
        return 0
