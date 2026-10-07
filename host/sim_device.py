#!/usr/bin/env python3
"""Simulated KMAN device, for testing the host tools without hardware.

Real-time virtual serial port (Linux/macOS), then point the logger at it:
    python host/sim_device.py --pty
    python host/logger.py --port /dev/pts/N

Generate files offline (fast):
    python host/sim_device.py --write-session data/sim.parquet     # + .bin + .labels.jsonl
    python host/sim_device.py --write-session data/sim_abd.parquet --abdomen

Labels in generated sessions are the simulator's ground truth, applied the same
way as key presses (from the next frame).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from kman_host import protocol as P
from kman_host import simulator as sim
from kman_host.labels import label_name
from kman_host.session_io import SessionWriter
from kman_host.stream import StreamDecoder


def make_session(args) -> sim.SimSession:
    cfg = sim.SimConfig(seed=args.seed, abdomen=args.abdomen, neck_imu=args.neck_imu, fs=args.rate)
    if args.wrap:
        cfg.t0_us = 2**32 - 5_000_000  # timestamp wraps 5 s in
    scenario = [(lab, d * args.time_scale) for lab, d in sim.DEFAULT_SCENARIO]
    return sim.generate(cfg, scenario)


def write_session(args, s: sim.SimSession) -> int:
    out = Path(args.write_session)
    dec = StreamDecoder()
    dec.schedule_labels(s.label_events())
    w = SessionWriter(out, raw=True, extra_meta={"source": "simulator", "seed": args.seed})
    for ev in s.label_events():
        w.write_label_event(ev)
    for _, fr in s.stream(drop_prob=args.drop_prob, corrupt_prob=args.corrupt_prob, seed=args.seed):
        w.write_raw(fr)
        dec.feed(fr, host_time=0.0)
        if dec.new_header:
            w.set_header(dec.header)
            dec.new_header = False
        if dec.pending_rows() >= 2000:
            w.write_batch(dec.drain())
    b = dec.drain()
    if b is not None:
        w.write_batch(b)
    w.close(dec.headers, dec.label_events_dicts(), dec.summary(), notes="synthetic session (simulator)")
    print(f"wrote {w.rows_written} samples ({s.n / s.cfg.fs:.0f} s) to {out} (+ .bin, .labels.jsonl); "
          f"lost {dec.stats.dropped}, crc errors {dec.parser.stats.crc_errors}")
    return 0


def run_pty(args, s: sim.SimSession) -> int:
    if os.name == "nt":
        print("--pty needs Linux/macOS. On Windows use a virtual COM pair (com0com) with --port.", file=sys.stderr)
        return 2
    import tty

    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    print(f"virtual device on {os.ttyname(slave)}  ({s.n / s.cfg.fs:.0f} s scenario, Ctrl-C to stop)", flush=True)

    def write_all(data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(master, view)
                view = view[n:]
            except BlockingIOError:
                time.sleep(0.001)

    gen = s.stream(drop_prob=args.drop_prob, corrupt_prob=args.corrupt_prob, seed=args.seed)
    write_all(next(gen)[1])  # header
    t0 = time.monotonic()
    last_label = None
    try:
        for i, fr in gen:
            due = t0 + (i + 1) / s.cfg.fs
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                cmd = os.read(master, 64)
            except (BlockingIOError, OSError):
                cmd = b""
            if P.CMD_SEND_HEADER in cmd:
                write_all(s.header_frame())
            write_all(fr)
            lab = int(s.labels[max(i, 0)])
            if lab != last_label:
                print(f"t={i / s.cfg.fs:7.1f} s  true activity: {lab} {label_name(lab)}", flush=True)
                last_label = lab
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pty", action="store_true", help="real-time virtual serial device")
    mode.add_argument("--write-session", metavar="OUT.parquet", help="write a session file offline")
    ap.add_argument("--abdomen", action="store_true", help="simulate an abdomen IMU")
    ap.add_argument("--neck-imu", action="store_true", help="simulate a neck IMU")
    ap.add_argument("--rate", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--time-scale", type=float, default=1.0, help="stretch/shrink the scenario durations")
    ap.add_argument("--drop-prob", type=float, default=0.0, help="probability of dropping each frame")
    ap.add_argument("--corrupt-prob", type=float, default=0.0, help="probability of corrupting each frame")
    ap.add_argument("--wrap", action="store_true", help="start the device clock 5 s before 32-bit wrap")
    args = ap.parse_args(argv)
    s = make_session(args)
    return run_pty(args, s) if args.pty else write_session(args, s)


if __name__ == "__main__":
    sys.exit(main())
