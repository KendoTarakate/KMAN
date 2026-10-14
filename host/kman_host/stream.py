"""Turns a byte stream from the device into timestamped, labelled sample batches.

Shared by the logger (live serial), replay (raw .bin files), the simulator and
the live viewer, so every consumer sees identical decoding, gap accounting and
label alignment.

Time base
    ``t_us`` is the device's 32-bit microsecond tick time unwrapped to int64.
    ``seq`` is the device tick counter unwrapped to int64; consecutive frames
    differ by exactly 1 unless frames were lost (device overrun, TX-ring drop,
    USB/host loss, CRC error). Gap size is derived from the timestamp delta,
    which is unambiguous for gaps up to 35 minutes, and cross-checked with the
    16-bit sequence number.

Labels
    A label change takes effect from the next frame after the key press:
    ``seq = last_seq + 1``. Replay applies events by seq, so live recording and
    rebuilds from raw data produce identical label columns.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass

import numpy as np

from . import protocol as P
from .labels import label_name


class LayoutChanged(Exception):
    """The device announced a different channel layout mid-session."""


@dataclass
class StreamStats:
    frames_data: int = 0
    frames_before_header: int = 0
    dropped: int = 0  # missing ticks inside a segment
    duplicates: int = 0
    seq_mismatch: int = 0
    bad_size: int = 0
    segments: int = 0
    restarts_detected: int = 0  # clock restarts found in the data (no header announced them)
    headers: int = 0
    status_frames: int = 0
    log_messages: int = 0

    def drop_pct(self) -> float:
        expected = self.frames_data + self.dropped
        return 100.0 * self.dropped / expected if expected else 0.0


@dataclass
class LabelEvent:
    seq: int | None  # first tick the label applies to (None: from the first frame)
    t_us: int | None
    host_time: float
    label: int
    name: str


@dataclass
class GapEvent:
    seq: int  # first missing tick
    t_us: int
    missing: int


class StreamDecoder:
    def __init__(self, keep_gap_events: int = 10000):
        self.parser = P.FrameParser()
        self.stats = StreamStats()
        self.header: P.SessionHeader | None = None
        self.headers: list[dict] = []
        self.status: P.DeviceStatus | None = None
        self.status_first: P.DeviceStatus | None = None
        self.logs: deque[str] = deque(maxlen=200)
        self.gaps: list[GapEvent] = []
        self._keep_gaps = keep_gap_events
        self.label = 0
        self.label_events: list[LabelEvent] = []
        self._pending_labels: list[LabelEvent] = []  # replay: events applied by seq
        self.segment = -1
        self._last_t32: int | None = None
        self._last_seq16 = 0
        self.last_t64: int | None = None
        self.last_seq64: int | None = None
        self._last_host_time = 0.0
        self._seg_gap_host = 0.0
        self._rows: list[np.ndarray] = []
        self._row_host: list[float] = []
        self._row_meta: list[tuple[int, int, int, int]] = []  # t64, seq64, segment, label
        self.new_header = False  # set when a header (re)defines the layout; consumer clears
        self.events: deque[str] = deque(maxlen=1000)  # human-readable notices for the UI

    # ------------------------------------------------------------------ input

    def feed(self, data: bytes, host_time: float | None = None) -> list[P.Frame]:
        """Parse bytes; returns the frames (for re-broadcasting)."""
        host_time = time.time() if host_time is None else host_time
        frames = self.parser.feed(data)
        for f in frames:
            self.handle_frame(f, host_time)
        return frames

    def handle_frame(self, f: P.Frame, host_time: float) -> None:
        if f.type == P.TYPE_DATA:
            self._on_data(f.body, host_time)
        elif f.type == P.TYPE_HEADER:
            self._on_header(f.body, host_time)
        elif f.type == P.TYPE_STATUS:
            st = P.decode_status(f.body)
            if st:
                self.stats.status_frames += 1
                self.status = st
                if self.status_first is None:
                    self.status_first = st
        elif f.type == P.TYPE_LOG:
            self.stats.log_messages += 1
            msg = f.body.decode("utf-8", "replace")
            self.logs.append(msg)
            self.events.append(f"device: {msg}")
        elif f.type == P.TYPE_LABEL and f.body:
            # Viewer side: logger announces the current label.
            self.label = f.body[0]

    def _on_header(self, body: bytes, host_time: float) -> None:
        hdr = P.SessionHeader.from_json(body)
        self.stats.headers += 1
        if self.header is None:
            self.header = hdr
            self.headers.append(hdr.raw)
            self.segment = 0
            self.stats.segments = 1
            self.new_header = True
            return
        if hdr.layout_key() != self.header.layout_key():
            raise LayoutChanged(
                f"channel layout changed: {self.header.channel_names} -> {hdr.channel_names}"
            )
        restarted, why = self._new_stream(hdr)
        if restarted:
            self.header = hdr
            self.headers.append(hdr.raw)
            self._start_segment(host_time, f"new stream ({why})")

    # A re-sent header (answer to 'H') is built while streaming, so its timestamp
    # sits just after the last frame received; allow the TX-ring backlog (<3 s) plus margin.
    HEADER_LEAD_MAX_US = 10_000_000
    HEADER_LAG_MAX_US = 1_000_000
    # A backwards step larger than this is a restarted device clock, not a duplicate.
    BACKWARD_RESTART_US = 1_000_000

    def _new_stream(self, hdr: P.SessionHeader) -> tuple[bool, str]:
        """Does this header start a new device stream (re-open, re-enumeration or reboot)?"""
        old = self.header.raw
        for key in ("device_id", "boot_id", "stream_id"):
            if hdr.raw.get(key) != old.get(key):
                return True, f"{key} changed"
        # Same ids (e.g. firmware without boot_id that rebooted and reused stream_id 1):
        # a device clock far from the running stream means the device restarted.
        t_hdr = hdr.raw.get("t_us_at_header")
        if t_hdr is not None and self._last_t32 is not None:
            ahead = (int(t_hdr) - self._last_t32) & 0xFFFFFFFF
            if not (ahead <= self.HEADER_LEAD_MAX_US or ahead >= 0x100000000 - self.HEADER_LAG_MAX_US):
                return True, "device clock restarted"
        return False, ""

    def _start_segment(self, host_time: float, why: str) -> None:
        """Next DATA frame starts a new segment; its time axis is bridged with the host-measured gap."""
        self.segment += 1
        self.stats.segments += 1
        self._last_t32 = None
        self._seg_gap_host = max(0.0, host_time - self._last_host_time)
        self.events.append(f"{why}: segment {self.segment}")

    def _on_data(self, body: bytes, host_time: float) -> None:
        if self.header is None:
            self.stats.frames_before_header += 1
            return
        rec = P.decode_data(body, self.header.data_dtype())
        if rec is None:
            self.stats.bad_size += 1
            return
        t32 = int(rec["t_us"][0])
        seq16 = int(rec["seq"][0])
        period = self.header.period_us

        if self._last_t32 is None:
            if self.last_t64 is None:  # very first frame of the session
                t64, seq64 = t32, seq16
            else:  # first frame of a new segment
                nt = max(1, int(round(self._seg_gap_host * 1e6 / period)))
                t64 = self.last_t64 + nt * period
                seq64 = self.last_seq64 + nt
        else:
            dt = (t32 - self._last_t32) & 0xFFFFFFFF
            if dt >= 0x80000000 and 0x100000000 - dt > self.BACKWARD_RESTART_US:
                # Clock went backwards by more than a second: the device restarted and
                # we missed (or it never sent) a header. Keep the data in a new segment.
                self._start_segment(host_time, "device clock jumped back")
                self.stats.restarts_detected += 1
                return self._on_data(body, host_time)
            if dt == 0 or dt >= 0x80000000:
                self.stats.duplicates += 1
                return
            nt = int(round(dt / period))
            if nt <= 0:
                self.stats.duplicates += 1
                return
            if ((seq16 - self._last_seq16) & 0xFFFF) != (nt & 0xFFFF):
                self.stats.seq_mismatch += 1
            if nt > 1:
                missing = nt - 1
                self.stats.dropped += missing
                if len(self.gaps) < self._keep_gaps:
                    self.gaps.append(GapEvent(self.last_seq64 + 1, self.last_t64 + period, missing))
                if missing >= 2:
                    self.events.append(f"gap: {missing} frames lost at t={self.last_t64 / 1e6:.2f} s")
            t64 = self.last_t64 + dt
            seq64 = self.last_seq64 + nt

        self._apply_pending_labels(seq64)
        self._last_t32, self._last_seq16 = t32, seq16
        self.last_t64, self.last_seq64 = t64, seq64
        self._last_host_time = host_time
        self.stats.frames_data += 1
        self._rows.append(rec)
        self._row_host.append(host_time)
        self._row_meta.append((t64, seq64, self.segment, self.label))

    # ------------------------------------------------------------------ labels

    def set_label(self, code: int, host_time: float | None = None) -> LabelEvent:
        """Live labelling: applies from the next frame."""
        host_time = time.time() if host_time is None else host_time
        nxt_seq = None if self.last_seq64 is None else self.last_seq64 + 1
        nxt_t = None
        if self.last_t64 is not None and self.header is not None:
            nxt_t = self.last_t64 + self.header.period_us
        ev = LabelEvent(nxt_seq, nxt_t, host_time, int(code), label_name(code))
        self.label = int(code)
        self.label_events.append(ev)
        return ev

    def schedule_labels(self, events: list[dict]) -> None:
        """Replay: apply recorded label events when their seq is reached."""
        evs = [LabelEvent(e.get("seq"), e.get("t_us"), e.get("host_time", 0.0), int(e["label"]),
                          e.get("name", label_name(e["label"]))) for e in events]
        evs.sort(key=lambda e: -1 if e.seq is None else e.seq)
        self._pending_labels = evs

    def _apply_pending_labels(self, seq64: int) -> None:
        while self._pending_labels and (self._pending_labels[0].seq is None or self._pending_labels[0].seq <= seq64):
            ev = self._pending_labels.pop(0)
            self.label = ev.label
            self.label_events.append(ev)

    # ------------------------------------------------------------------ output

    def pending_rows(self) -> int:
        return len(self._rows)

    def drain(self) -> dict[str, np.ndarray] | None:
        """Decoded samples since the last drain, as columns, or None if empty."""
        if not self._rows:
            return None
        recs = np.concatenate(self._rows)
        meta = np.array(self._row_meta, dtype=np.int64)
        out: dict[str, np.ndarray] = {
            "t_us": meta[:, 0],
            "seq": meta[:, 1],
            "segment": meta[:, 2].astype(np.int16),
            "valid": recs["valid"].astype(np.uint8),
        }
        for name in self.header.channel_names:
            out[name] = recs[name].astype(np.int16)
        out["label"] = meta[:, 3].astype(np.int8)
        out["host_time"] = np.array(self._row_host, dtype=np.float64)
        self._rows.clear()
        self._row_host.clear()
        self._row_meta.clear()
        return out

    # ------------------------------------------------------------------ summary

    def summary(self) -> dict:
        ps = self.parser.stats
        d = {
            "stream": asdict(self.stats),
            "drop_pct": self.stats.drop_pct(),
            "parser": asdict(ps),
            "gaps": [asdict(g) for g in self.gaps],
        }
        if self.status:
            d["device_status_last"] = self.status.as_dict()
        if self.status_first:
            d["device_status_first"] = self.status_first.as_dict()
        return d

    def summary_json(self) -> str:
        return json.dumps(self.summary())

    def label_events_dicts(self) -> list[dict]:
        return [asdict(e) for e in self.label_events]
