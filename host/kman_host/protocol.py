"""Wire protocol: framing, CRC, session header and payload decoding.

Mirror of firmware/src/protocol.h. Frame layout::

    0xAA 0x55 | len u16le | type u8 | body[len-1] | crc u16le

``len`` counts type+body; the CRC (CRC-16/CCITT-FALSE) covers len, type and body.
"""

from __future__ import annotations

import binascii
import json
import struct
from dataclasses import dataclass, field
import numpy as np

SYNC = b"\xaa\x55"
OVERHEAD = 7
MAX_LEN = 4096

TYPE_DATA = 0x01
TYPE_HEADER = 0x02
TYPE_STATUS = 0x03
TYPE_LOG = 0x04
# Host-side only (logger -> viewers over UDP); never sent by the device.
TYPE_LABEL = 0x10

CMD_SEND_HEADER = b"H"
CMD_SEND_STATUS = b"S"

DATA_PREFIX = struct.Struct("<IHB")  # t_us, seq, valid

STATUS_SLOTS = 4
_STATUS = struct.Struct("<B5I4IHHB")
STATUS_FLAG_HFXO = 0x01
STATUS_FLAG_SENSOR_ERROR = 0x02


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF). binascii.crc_hqx is exactly this, in C."""
    return binascii.crc_hqx(data, crc)


def encode_frame(ftype: int, body: bytes) -> bytes:
    length = len(body) + 1
    if length > MAX_LEN:
        raise ValueError(f"frame body too large ({len(body)} bytes)")
    head = struct.pack("<HB", length, ftype)
    return SYNC + head + body + struct.pack("<H", crc16(head + body))


@dataclass
class Frame:
    type: int
    body: bytes
    raw: bytes  # complete frame bytes (for re-broadcasting)


@dataclass
class ParserStats:
    frames: int = 0
    crc_errors: int = 0
    bad_length: int = 0
    bytes_discarded: int = 0  # bytes skipped while searching for sync


class FrameParser:
    """Incremental frame parser with resynchronisation.

    Feed arbitrary byte chunks; complete, CRC-valid frames are returned. On a CRC
    or length error the parser advances one byte past the false sync and
    searches again, so a corrupted frame costs exactly that frame.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[Frame]:
        out: list[Frame] = []
        buf = self._buf
        buf += data
        pos = 0
        n = len(buf)
        while True:
            i = buf.find(SYNC, pos)
            if i < 0:
                # Keep a trailing 0xAA: it may be the first sync byte of the next chunk.
                # (Never re-keep bytes already consumed: pos may sit right after a frame ending in 0xAA.)
                keep_from = n - 1 if n - 1 >= pos and buf[-1] == 0xAA else n
                self.stats.bytes_discarded += max(0, keep_from - pos)
                pos = keep_from
                break
            self.stats.bytes_discarded += i - pos
            pos = i
            if n - pos < 5:
                break
            length = buf[pos + 2] | (buf[pos + 3] << 8)
            if length < 1 or length > MAX_LEN:
                self.stats.bad_length += 1
                pos += 1
                continue
            end = pos + 4 + length + 2
            if end > n:
                break
            crc_rx = buf[end - 2] | (buf[end - 1] << 8)
            if crc16(bytes(buf[pos + 2 : end - 2])) != crc_rx:
                self.stats.crc_errors += 1
                pos += 1
                continue
            self.stats.frames += 1
            raw = bytes(buf[pos:end])
            out.append(Frame(type=raw[4], body=raw[5:-2], raw=raw))
            pos = end
        del buf[:pos]
        return out


# --------------------------------------------------------------------------- header


@dataclass
class Channel:
    name: str
    slot: str
    bit: int
    unit: str
    scale: float


@dataclass
class SessionHeader:
    raw: dict
    channels: list[Channel] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str | bytes) -> "SessionHeader":
        d = json.loads(text)
        if d.get("type") != "kman_session_header":
            raise ValueError("not a kman session header")
        if "error" in d:
            raise ValueError(f"device reported header error: {d['error']}")
        chans = [Channel(c["name"], c["slot"], int(c["bit"]), c["unit"], float(c["scale"])) for c in d["channels"]]
        return cls(raw=d, channels=chans)

    @property
    def sample_rate_hz(self) -> float:
        return float(self.raw["sample_rate_hz"])

    @property
    def period_us(self) -> int:
        return int(self.raw["period_us"])

    @property
    def stream_id(self) -> int:
        return int(self.raw.get("stream_id", 0))

    @property
    def present_mask(self) -> int:
        return int(self.raw.get("present_mask", 0))

    @property
    def channel_names(self) -> list[str]:
        return [c.name for c in self.channels]

    def layout_key(self) -> tuple:
        """Identity of the payload layout; two headers with equal keys decode identically."""
        return tuple((c.name, c.scale) for c in self.channels) + (self.period_us,)

    def data_dtype(self) -> np.dtype:
        fields = [("t_us", "<u4"), ("seq", "<u2"), ("valid", "u1")]
        fields += [(c.name, "<i2") for c in self.channels]
        return np.dtype(fields)

    def slots(self) -> dict[str, dict]:
        return {s["slot"]: s for s in self.raw.get("sensors", [])}

    def slot_present(self, slot: str) -> bool:
        return self.slots().get(slot, {}).get("status") == "ok"

    def describe(self) -> str:
        lines = [
            f"board={self.raw.get('board')} fw={self.raw.get('fw_version')} "
            f"device={self.raw.get('device_id')} rate={self.sample_rate_hz:g} Hz "
            f"stream={self.stream_id}",
            f"clock: {self.raw.get('clock', {}).get('hfclk', '?')}",
        ]
        for s in self.raw.get("sensors", []):
            line = f"  {s['slot']:<12} {s['status']:<15} {s.get('model', '')}"
            if s.get("error"):
                line += f"  ERROR: {s['error']}"
            if s.get("warning"):
                line += f"  WARNING: {s['warning']}"
            lines.append(line)
        lines.append("  channels: " + ", ".join(self.channel_names))
        return "\n".join(lines)


def decode_data(body: bytes, dtype: np.dtype) -> np.ndarray | None:
    """One DATA body -> 1-element structured array, or None if the size doesn't match."""
    if len(body) != dtype.itemsize:
        return None
    return np.frombuffer(body, dtype=dtype, count=1)


@dataclass
class DeviceStatus:
    t_us: int
    ticks: int
    frames_sent: int
    overruns: int
    tx_drops: int
    read_errors: tuple[int, ...]
    max_latency_us: int
    tx_high_water: int
    flags: int

    @property
    def hfxo(self) -> bool:
        return bool(self.flags & STATUS_FLAG_HFXO)

    @property
    def sensor_error(self) -> bool:
        return bool(self.flags & STATUS_FLAG_SENSOR_ERROR)

    def as_dict(self) -> dict:
        return {
            "t_us": self.t_us,
            "ticks": self.ticks,
            "frames_sent": self.frames_sent,
            "overruns": self.overruns,
            "tx_drops": self.tx_drops,
            "read_errors": list(self.read_errors),
            "max_latency_us": self.max_latency_us,
            "tx_high_water": self.tx_high_water,
            "hfxo": self.hfxo,
            "sensor_error": self.sensor_error,
            "flags": self.flags,
        }


def decode_status(body: bytes) -> DeviceStatus | None:
    if len(body) != _STATUS.size or body[0] != 1:
        return None
    v = _STATUS.unpack(body)
    return DeviceStatus(
        t_us=v[1],
        ticks=v[2],
        frames_sent=v[3],
        overruns=v[4],
        tx_drops=v[5],
        read_errors=tuple(v[6:10]),
        max_latency_us=v[10],
        tx_high_water=v[11],
        flags=v[12],
    )


def encode_status(s: DeviceStatus) -> bytes:
    return _STATUS.pack(
        1, s.t_us, s.ticks, s.frames_sent, s.overruns, s.tx_drops, *s.read_errors, s.max_latency_us, s.tx_high_water, s.flags
    )
