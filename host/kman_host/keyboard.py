"""Non-blocking single-key input for the terminal (Windows, macOS, Linux)."""

from __future__ import annotations

import os
import sys


class KeyReader:
    """Context manager; poll() returns the characters typed since the last call."""

    def __init__(self) -> None:
        self._posix_state = None
        self.enabled = sys.stdin is not None and sys.stdin.isatty()

    def __enter__(self) -> "KeyReader":
        if self.enabled and os.name != "nt":
            import termios
            import tty

            fd = sys.stdin.fileno()
            self._posix_state = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._posix_state is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._posix_state)
            self._posix_state = None

    def poll(self) -> list[str]:
        if not self.enabled:
            return []
        keys: list[str] = []
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # function/arrow key prefix: swallow the scan code
                    msvcrt.getwch()
                    continue
                keys.append(ch)
        else:
            import select

            fd = sys.stdin.fileno()
            while select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 32)
                if not data:
                    break
                keys.extend(data.decode("utf-8", "ignore"))
        return keys
