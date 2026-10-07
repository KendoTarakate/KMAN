"""Localhost UDP links between the logger (owner of the serial port) and viewers.

logger -> viewer (default port 51515): verbatim device frames, the latest HEADER
    re-sent every 2 s for late joiners, and host LABEL frames (type 0x10).
viewer -> logger (default port 51516): ASCII commands, e.g. b"LABEL 3".

UDP is fire-and-forget, so a slow or absent viewer can never stall recording.
"""

from __future__ import annotations

import socket

DEFAULT_PORT = 51515
MAX_DATAGRAM = 8192


class UdpSender:
    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, data: bytes) -> None:
        for i in range(0, len(data), MAX_DATAGRAM):
            try:
                self.sock.sendto(data[i : i + MAX_DATAGRAM], self.addr)
            except OSError:
                pass  # nobody listening / transient; never block the logger

    def close(self) -> None:
        self.sock.close()


class UdpReceiver:
    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.setblocking(False)

    def poll(self) -> list[bytes]:
        out = []
        while True:
            try:
                data, _ = self.sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:  # e.g. WSAECONNRESET on Windows
                break
            out.append(data)
        return out

    def close(self) -> None:
        self.sock.close()


def parse_label_command(msg: bytes) -> int | None:
    parts = msg.decode("ascii", "ignore").strip().split()
    if len(parts) == 2 and parts[0] == "LABEL" and parts[1].isdigit():
        return int(parts[1])
    return None
