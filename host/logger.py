#!/usr/bin/env python3
"""Record a KMAN session from the device, with live keyboard labelling.

    python host/logger.py --port COM5 --out data/s01_rest.parquet
    python host/logger.py --port /dev/ttyACM0                       # auto-named file in data/
    python host/logger.py --replay data/s01_rest.bin --out data/s01_rebuilt.parquet
    python host/logger.py --list-ports

Keys while recording: 0-9 set the activity label (see below), q quit, ? help.
Writes <out>.parquet, plus <out>.bin (raw bytes, crash-safe) and
<out>.labels.jsonl. Decoded frames are re-broadcast on UDP for live_view.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from kman_host import protocol as P
from kman_host.keyboard import KeyReader
from kman_host.labels import LABELS, help_text, label_name
from kman_host.session_io import SessionWriter, read_label_events, sidecar_paths
from kman_host.stream import LayoutChanged, StreamDecoder
from kman_host.udp_bus import DEFAULT_PORT, UdpReceiver, UdpSender, parse_label_command

REPO = Path(__file__).resolve().parents[1]


def default_out() -> Path:
    return REPO / "data" / (dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".parquet")


class Recorder:
    """Decoder + writer glue shared by live recording and replay."""

    def __init__(self, out: Path, raw: bool, extra_meta: dict, udp_port: int | None,
                 label_events: list[dict] | None = None):
        self.dec = StreamDecoder()
        self.writer = SessionWriter(out, raw=raw, extra_meta=extra_meta, label_events=label_events)
        self.udp = UdpSender(udp_port) if udp_port else None
        self.last_header_raw: bytes | None = None
        self.notes: list[str] = []

    def feed(self, data: bytes, host_time: float | None = None) -> None:
        self.writer.write_raw(data)
        frames = self.dec.feed(data, host_time)
        if self.udp and frames:
            self.udp.send(b"".join(f.raw for f in frames))
        for f in frames:
            if f.type == P.TYPE_HEADER:
                # Re-announced to viewers every 2 s. Drop the device timestamp: it goes
                # stale, and the viewer's decoder would read it as a clock restart.
                hdr = json.loads(f.body)
                hdr.pop("t_us_at_header", None)
                self.last_header_raw = P.encode_frame(P.TYPE_HEADER, json.dumps(hdr).encode())
        if self.dec.new_header:
            self.writer.set_header(self.dec.header)
            self.dec.new_header = False
            print("\n[header]\n" + self.dec.header.describe(), flush=True)
        if self.dec.pending_rows() >= 100:
            self.writer.write_batch(self.dec.drain())

    def set_label(self, code: int) -> None:
        ev = self.dec.set_label(code)
        self.writer.write_label_event(asdict(ev))
        self.announce_label()

    def announce_label(self) -> None:
        if self.udp:
            self.udp.send(P.encode_frame(P.TYPE_LABEL, bytes([self.dec.label])))

    def reannounce_header(self) -> None:
        if self.udp and self.last_header_raw:
            self.udp.send(self.last_header_raw)
            self.announce_label()

    def close(self) -> dict:
        if self.writer.header is None and self.dec.header is not None:
            # feed() was interrupted (LayoutChanged) before it could hand the header over.
            self.writer.set_header(self.dec.header)
        batch = self.dec.drain()
        if batch is not None and self.writer.header is not None:
            self.writer.write_batch(batch)
        summary = self.dec.summary()
        self.writer.close(self.dec.headers, self.dec.label_events_dicts(), summary, notes="\n".join(self.notes))
        if self.udp:
            self.udp.close()
        return summary


def status_line(rec: Recorder, t_start: float, rate: float) -> str:
    st, ps = rec.dec.stats, rec.dec.parser.stats
    el = int(time.time() - t_start)
    s = (f"[{el // 3600:02d}:{el // 60 % 60:02d}:{el % 60:02d}] frames {st.frames_data:>7}  {rate:5.1f} Hz  "
         f"lost {st.dropped} ({st.drop_pct():.3f}%)  crc {ps.crc_errors}")
    d = rec.dec.status
    if d:
        s += (f"  dev[ovr {d.overruns} txdrop {d.tx_drops} i2c {sum(d.read_errors)} "
              f"lat {d.max_latency_us / 1000:.1f}ms{'' if d.hfxo else ' NO-XTAL'}]")
    s += f"  | {rec.dec.label} {label_name(rec.dec.label)}"
    return s


def print_summary(summary: dict, out: Path, rows: int) -> None:
    st, ps = summary["stream"], summary["parser"]
    print(f"\nSaved {rows} samples to {out}")
    print(f"  frames received {st['frames_data']}, lost {st['dropped']} ({summary['drop_pct']:.4f}%), "
          f"CRC errors {ps['crc_errors']}, resync bytes {ps['bytes_discarded']}, segments {st['segments']}")
    dev = summary.get("device_status_last")
    if dev:
        print(f"  device: overruns {dev['overruns']}, tx drops {dev['tx_drops']}, read errors {dev['read_errors']}, "
              f"HF crystal {'yes' if dev['hfxo'] else 'NO'}")
    if summary["drop_pct"] >= 0.1:
        print("  WARNING: frame loss >= 0.1% (acceptance limit). See README 'Troubleshooting drops'.")


def run_live(args) -> int:
    import serial

    out = Path(args.out) if args.out else default_out()
    if out.exists() and not args.force:
        print(f"{out} exists; use --force to overwrite", file=sys.stderr)
        return 2
    meta = {"source": "serial", "port": args.port, "subject": args.subject, "argv": sys.argv}
    rec = Recorder(out, raw=not args.no_raw, extra_meta=meta, udp_port=None if args.no_udp else args.udp_port)
    if args.notes:
        rec.notes.append(args.notes)
    ctl = None
    if not args.no_udp:
        try:
            ctl = UdpReceiver(args.udp_port + 1)
        except OSError as e:
            print(f"(viewer label control disabled: {e})")

    def open_port():
        ser = serial.Serial(args.port, 115200, timeout=0.02)
        if hasattr(ser, "set_buffer_size"):
            ser.set_buffer_size(rx_size=1 << 16)  # Windows only
        ser.write(P.CMD_SEND_HEADER)
        return ser

    def handle_inputs(keys) -> bool:
        """Keys and viewer label commands; returns False when the user asked to quit."""
        go = True
        for k in keys.poll():
            if k.isdigit():
                rec.set_label(int(k))
                print(f"\n[label -> {k} {label_name(int(k))}]", flush=True)
            elif k in ("q", "Q"):
                go = False
            elif k == "?":
                print("\n" + help_text(), flush=True)
        if ctl:
            for msg in ctl.poll():
                code = parse_label_command(msg)
                if code is not None and code in LABELS:
                    rec.set_label(code)
                    print(f"\n[label (viewer) -> {code} {label_name(code)}]", flush=True)
        return go

    print(f"Recording to {out}\nLabels: {help_text()}\nKeys: 0-9 label, q quit, ? help", flush=True)
    ser = open_port()
    t_start = time.time()
    last_print = last_announce = last_rate_t = time.time()
    last_rate_n = 0
    rate = 0.0
    header_asked = time.time()
    running = True
    try:
        with KeyReader() as keys:
            while running:
                try:
                    data = ser.read(max(1, ser.in_waiting))
                except OSError as e:  # SerialException is an OSError; POSIX in_waiting raises a bare OSError

                    print(f"\n[serial error: {e}] reconnecting... (q to stop)", flush=True)
                    ser.close()
                    next_try = time.time() + 1.0
                    while running:
                        # Labels pressed now apply from the first frame after the reconnect.
                        running = handle_inputs(keys)
                        if args.duration and time.time() - t_start >= args.duration:
                            running = False
                        if not running or time.time() < next_try:
                            time.sleep(0.05)
                            continue
                        next_try = time.time() + 1.0
                        try:
                            ser = open_port()
                            print("[reconnected]", flush=True)
                            rec.notes.append(f"reconnected at {time.time():.3f}")
                            break
                        except OSError:
                            pass
                    continue
                if data:
                    try:
                        rec.feed(data)
                    except LayoutChanged as e:
                        print(f"\n[{e}] -- stopping; start a new recording for the new firmware.", flush=True)
                        break

                now = time.time()
                if rec.dec.header is None and now - header_asked > 2.0:
                    header_asked = now
                    try:
                        ser.write(P.CMD_SEND_HEADER)
                    except OSError:
                        pass  # the read path handles the disconnect
                    print(f"\n[no session header yet; {rec.dec.parser.stats.frames} frames, "
                          f"{rec.dec.stats.frames_before_header} data frames waiting] requested again", flush=True)

                if not handle_inputs(keys):
                    running = False

                while rec.dec.events:
                    print("\n[" + rec.dec.events.popleft() + "]", flush=True)

                if now - last_rate_t >= 2.0:
                    n = rec.dec.stats.frames_data
                    rate = (n - last_rate_n) / (now - last_rate_t)
                    last_rate_n, last_rate_t = n, now
                if now - last_print >= 0.5:
                    last_print = now
                    sys.stdout.write("\r" + status_line(rec, t_start, rate)[:160] + "\x1b[K")
                    sys.stdout.flush()
                if now - last_announce >= 2.0:
                    last_announce = now
                    rec.reannounce_header()
                if args.duration and now - t_start >= args.duration:
                    running = False
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.close()
        except Exception:
            pass
        if ctl:
            ctl.close()
        summary = rec.close()
        print_summary(summary, out, rec.writer.rows_written)
    return 0


def run_replay(args) -> int:
    src = Path(args.replay)
    out = Path(args.out) if args.out else src.with_name(src.stem + "_replay.parquet")
    if out.exists() and not args.force:
        print(f"{out} exists; use --force to overwrite", file=sys.stderr)
        return 2
    labels_path = Path(args.labels) if args.labels else sidecar_paths(src.with_suffix(".parquet"))[1]
    events = read_label_events(labels_path)
    # The output's .labels.jsonl gets the same events. When --out has the source's stem
    # this rewrites the very file just read, so recovering a session never loses its labels.
    rec = Recorder(out, raw=False, extra_meta={"source": "replay", "replay_of": str(src)}, udp_port=None,
                   label_events=events)
    rec.dec.schedule_labels(events)
    data = src.read_bytes()
    try:
        for i in range(0, len(data), 1 << 16):
            rec.feed(data[i : i + (1 << 16)], host_time=0.0)
    except LayoutChanged as e:
        print(f"[{e}] -- replay stopped there; the output holds the data before the change.", file=sys.stderr)
    summary = rec.close()
    print(f"replayed {len(data)} bytes with {len(events)} label events from {labels_path}")
    print_summary(summary, out, rec.writer.rows_written)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (COMx, /dev/ttyACM0, /dev/cu.usbmodem...)")
    ap.add_argument("--out", help="output .parquet (default: data/<timestamp>.parquet)")
    ap.add_argument("--replay", help="rebuild a session from a raw .bin capture instead of a port")
    ap.add_argument("--labels", help="labels .jsonl for --replay (default: sidecar of the .bin)")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until q)")
    ap.add_argument("--subject", default="", help="subject/session id stored in metadata")
    ap.add_argument("--notes", default="", help="free-text notes stored in metadata")
    ap.add_argument("--udp-port", type=int, default=DEFAULT_PORT, help="UDP port for live_view (control = port+1)")
    ap.add_argument("--no-udp", action="store_true", help="do not broadcast to live_view")
    ap.add_argument("--no-raw", action="store_true", help="do not write the raw .bin capture")
    ap.add_argument("--force", action="store_true", help="overwrite existing output")
    ap.add_argument("--list-ports", action="store_true")
    args = ap.parse_args(argv)

    if args.list_ports:
        from serial.tools import list_ports

        for p in list_ports.comports():
            print(f"{p.device:<20} {p.description}  [{p.hwid}]")
        return 0
    if args.replay:
        return run_replay(args)
    if not args.port:
        ap.error("--port is required (or --replay / --list-ports)")
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
