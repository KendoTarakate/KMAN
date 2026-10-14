#!/usr/bin/env python3
"""Real-time scrolling view of the KMAN signals (pyqtgraph).

    python host/live_view.py                      # attach to a running logger.py (UDP)
    python host/live_view.py --port COM5          # straight from the device (nothing recorded)
    python host/live_view.py --sim [--abdomen]    # built-in simulator, no hardware
    python host/live_view.py --replay data/s01.bin --speed 4

Keys in the plot window: 0-9 set the label (forwarded to the logger when
attached over UDP), space pause, a toggle PC1 / 3 axes, d toggle flex
detrend, q or Esc quit.

The respiration trace is computed causally: per-axis 0.05-1 Hz band-pass of
the chest accelerometer (gravity tilt), projected on the first principal
component of the visible window (sign kept continuous between redraws), and
shown in degrees of tilt. It is a display proxy; the offline analysis uses
the full complementary-filter pipeline.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from scipy import signal

from kman_host import protocol as P
from kman_host.labels import LABEL_COLORS_DARK, LABELS, label_name
from kman_host.stream import LayoutChanged, StreamDecoder
from kman_host.udp_bus import DEFAULT_PORT, UdpReceiver, UdpSender

SURFACE = "#1a1a19"
TEXT = "#ffffff"
TEXT_2 = "#c3c2b7"
GRID_ALPHA = 0.15
SERIES = {"chest": "#3987e5", "abd": "#d95926", "flex": "#199e70", "motion": "#9085e9"}


# --------------------------------------------------------------------------- sources


class UdpSource:
    name = "logger (udp)"

    def __init__(self, port: int):
        self.rx = UdpReceiver(port)
        self.tx = UdpSender(port + 1)
        self.name = f"logger udp:{port}"

    def poll(self) -> bytes:
        return b"".join(self.rx.poll())

    def send_label(self, code: int) -> bool:
        self.tx.send(f"LABEL {code}".encode())
        return True  # logger echoes the label back in a LABEL frame


class SerialSource:
    def __init__(self, port: str):
        import serial

        self.ser = serial.Serial(port, 115200, timeout=0)
        self.ser.write(P.CMD_SEND_HEADER)
        self.name = f"serial {port} (not recording)"

    def poll(self) -> bytes:
        return self.ser.read(self.ser.in_waiting or 0)

    def send_label(self, code: int) -> bool:
        return False


class PacedFrameSource:
    """Plays a list of frames in real time (x speed) using the DATA timestamps."""

    def __init__(self, frames: list[bytes], data_index: list[int], fs: float, speed: float, name: str):
        self.frames = frames
        self.data_index = data_index  # per frame: running count of DATA frames up to it
        self.fs = fs
        self.speed = speed
        self.pos = 0
        self.t0 = time.monotonic()
        self.name = name

    def poll(self) -> bytes:
        due = (time.monotonic() - self.t0) * self.speed * self.fs
        out = []
        while self.pos < len(self.frames) and self.data_index[self.pos] <= due:
            out.append(self.frames[self.pos])
            self.pos += 1
        return b"".join(out)

    def send_label(self, code: int) -> bool:
        return False


def sim_source(abdomen: bool, speed: float) -> PacedFrameSource:
    from kman_host import simulator as sim

    s = sim.generate(sim.SimConfig(abdomen=abdomen))
    frames, idx = [], []
    last_label = None
    n_data = 0
    for i, fr in s.stream():
        if i >= 0 and s.labels[i] != last_label:  # announce ground truth as a label frame
            last_label = int(s.labels[i])
            frames.append(P.encode_frame(P.TYPE_LABEL, bytes([last_label])))
            idx.append(n_data)
        frames.append(fr)
        if i >= 0 and fr[4] == P.TYPE_DATA:
            n_data += 1
        idx.append(n_data)
    return PacedFrameSource(frames, idx, s.cfg.fs, speed, "simulator (labels = ground truth)")


def replay_source(path: str, speed: float) -> PacedFrameSource:
    parser = P.FrameParser()
    frames = parser.feed(Path(path).read_bytes())
    fs = 100.0
    out, idx = [], []
    n_data = 0
    for f in frames:
        if f.type == P.TYPE_HEADER:
            fs = P.SessionHeader.from_json(f.body).sample_rate_hz
        if f.type == P.TYPE_DATA:
            n_data += 1
        out.append(f.raw)
        idx.append(n_data)
    return PacedFrameSource(out, idx, fs, speed, f"replay {Path(path).name} x{speed:g}")


# --------------------------------------------------------------------------- processing


class Ring:
    def __init__(self, n: int, width: int = 1):
        self.buf = np.full((n, width), np.nan)
        self.n = n
        self.count = 0  # total appended

    def extend(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=float).reshape(len(x), -1)
        if len(x) >= self.n:
            self.count += len(x)
            # Keep the newest n rows with the oldest at count % n, where ordered() starts reading.
            self.buf[:] = np.roll(x[-self.n :], self.count % self.n, axis=0)
            return
        start = self.count % self.n
        first = min(len(x), self.n - start)
        self.buf[start : start + first] = x[:first]
        self.buf[: len(x) - first] = x[first:]
        self.count += len(x)

    def ordered(self) -> np.ndarray:
        if self.count < self.n:
            return self.buf[: self.count]
        s = self.count % self.n
        return np.concatenate([self.buf[s:], self.buf[:s]])


class LiveProcessor:
    def __init__(self, header: P.SessionHeader, window_s: float, hp_hz: float):
        self.h = header
        fs = header.sample_rate_hz
        self.fs = fs
        n = int(window_s * fs)
        names = header.channel_names
        scale = {c.name: c.scale for c in header.channels}
        bit = {c.name: c.bit for c in header.channels}
        self.groups: dict[str, list[str]] = {}
        for key, prefix in (("chest", "chest_a"), ("abd", "abd_a")):
            cols = [f"{prefix}{a}" for a in "xyz"]
            if all(c in names for c in cols):
                self.groups[key] = cols
        self.has_flex = "flex_raw" in names
        self.scale, self.bit = scale, bit
        self.sos_resp = signal.butter(2, [hp_hz, 1.0], btype="bandpass", fs=fs, output="sos")
        self.sos_motion = signal.butter(2, 1.5, btype="highpass", fs=fs, output="sos")
        self.zi: dict[str, np.ndarray] = {}
        self.last_valid: dict[str, np.ndarray] = {}
        self.t = Ring(n)
        self.resp = {k: Ring(n, 3) for k in self.groups}
        self.flex = Ring(n)
        self.flex_detr = Ring(n)
        self.motion = Ring(n)
        self.label = Ring(n)
        self.pc: dict[str, np.ndarray] = {}

    def _held(self, key: str, x: np.ndarray, ok: np.ndarray) -> np.ndarray:
        """Replace samples from frames flagged invalid with the last good value (keeps IIR state finite)."""
        last = self.last_valid.get(key, np.zeros(x.shape[1:]))
        if not ok.all():
            x = x.copy()
            for i in range(len(x)):
                if ok[i]:
                    last = x[i]
                else:
                    x[i] = last
        self.last_valid[key] = x[-1] if ok[-1] else last
        return x

    def _ema(self, key: str, x: np.ndarray, tau_s: float) -> np.ndarray:
        a = 1.0 - np.exp(-1.0 / (tau_s * self.fs))
        if key not in self.zi:
            self.zi[key] = np.array([(1 - a) * x[0]])
        y, self.zi[key] = signal.lfilter([a], [1.0, -(1.0 - a)], x, zi=self.zi[key])
        return y

    def _filt(self, key: str, sos: np.ndarray, x: np.ndarray) -> np.ndarray:
        if key not in self.zi:
            zi = signal.sosfilt_zi(sos)
            self.zi[key] = zi[:, :, None] * x[0][None, None, :]  # start in steady state
        y, self.zi[key] = signal.sosfilt(sos, x, axis=0, zi=self.zi[key])
        return y

    def process(self, b: dict[str, np.ndarray]) -> None:
        self.t.extend(b["t_us"] / 1e6)
        self.label.extend(b["label"])
        valid = b["valid"]
        for key, cols in self.groups.items():
            ok = (valid & (1 << self.bit[cols[0]])) != 0
            g = np.stack([b[c] * self.scale[c] for c in cols], axis=1)
            g = self._held(key, g, ok)
            self.resp[key].extend(self._filt(key + "_resp", self.sos_resp, g))
            if key == "chest":
                hp = self._filt("chest_motion", self.sos_motion, g)
                e = np.sum(hp**2, axis=1)
                self.motion.extend(np.sqrt(np.maximum(self._ema("motion", e, 0.5), 0)) * 1000.0)  # mg rms
        if self.has_flex:
            ok = (valid & (1 << self.bit["flex_raw"])) != 0
            f = self._held("flex", b["flex_raw"].astype(float)[:, None], ok)[:, 0]
            self.flex.extend(f)
            self.flex_detr.extend(f - self._ema("flex_base", f, 10.0))

    def resp_pc1_deg(self, key: str) -> np.ndarray:
        x = self.resp[key].ordered()
        if len(x) < 10:
            return np.zeros(len(x))
        xc = x - x.mean(axis=0)
        w, v = np.linalg.eigh(np.cov(xc.T))
        pc = v[:, -1]
        prev = self.pc.get(key)
        if prev is not None and float(pc @ prev) < 0:
            pc = -pc
        self.pc[key] = pc
        return np.degrees(xc @ pc)  # small-angle: g-fraction ~ radians

    def resp_axes_deg(self, key: str) -> np.ndarray:
        x = self.resp[key].ordered()
        return np.degrees(x - x.mean(axis=0)) if len(x) else x


# --------------------------------------------------------------------------- UI


def build_label_lut() -> np.ndarray:
    lut = np.zeros((256, 4), dtype=np.ubyte)
    lut[:, :] = (0x38, 0x38, 0x35, 255)  # label 0 / unknown: neutral
    for k, hexcol in LABEL_COLORS_DARK.items():
        lut[k] = (int(hexcol[1:3], 16), int(hexcol[3:5], 16), int(hexcol[5:7], 16), 255)
    return lut


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--udp", type=int, nargs="?", const=DEFAULT_PORT, help="attach to logger.py (default)")
    src.add_argument("--port", help="read the device directly (no recording)")
    src.add_argument("--sim", action="store_true", help="built-in simulator")
    src.add_argument("--replay", help="play back a raw .bin capture")
    ap.add_argument("--abdomen", action="store_true", help="simulator: add an abdomen IMU")
    ap.add_argument("--speed", type=float, default=1.0, help="replay/sim speed factor")
    ap.add_argument("--window", type=float, default=20.0, help="seconds shown")
    ap.add_argument("--hp", type=float, default=0.05, help="respiration high-pass corner (Hz)")
    ap.add_argument("--exit-after", type=float, default=0, help="quit after N seconds (testing)")
    ap.add_argument("--screenshot", help="save a PNG of the window when it closes")
    args = ap.parse_args(argv)

    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

    if args.port:
        source = SerialSource(args.port)
    elif args.sim:
        source = sim_source(args.abdomen, args.speed)
    elif args.replay:
        source = replay_source(args.replay, args.speed)
    else:
        source = UdpSource(args.udp or DEFAULT_PORT)

    pg.setConfigOptions(antialias=True, background=SURFACE, foreground=TEXT_2)
    app = pg.mkQApp("KMAN live view")

    state = {"paused": False, "axes": False, "detrend": True, "proc": None, "error": None}
    dec = StreamDecoder()

    class Window(QtWidgets.QWidget):
        def keyPressEvent(self, ev):  # noqa: N802 (Qt naming)
            k = ev.text()
            if k.isdigit():
                code = int(k)
                if not source.send_label(code):
                    dec.label = code  # standalone: label is display-only
            elif k == " ":
                state["paused"] = not state["paused"]
            elif k == "a":
                state["axes"] = not state["axes"]
            elif k == "d":
                state["detrend"] = not state["detrend"]
            elif k in ("q", "\x1b") or ev.key() == QtCore.Qt.Key.Key_Escape:
                self.close()

        def closeEvent(self, ev):  # noqa: N802 (Qt naming)
            if args.screenshot:  # however the window is closed
                self.grab().save(args.screenshot)
            super().closeEvent(ev)

    win = Window()
    win.setWindowTitle(f"KMAN live view - {source.name}")
    win.setStyleSheet(f"background-color: {SURFACE}; color: {TEXT};")
    vbox = QtWidgets.QVBoxLayout(win)
    top = QtWidgets.QHBoxLayout()
    label_lbl = QtWidgets.QLabel("waiting for session header...")
    label_lbl.setWordWrap(True)  # long error messages must not widen the window
    f = QtGui.QFont()
    f.setPointSize(20)
    f.setBold(True)
    label_lbl.setFont(f)
    stats_lbl = QtWidgets.QLabel("")
    stats_lbl.setStyleSheet(f"color: {TEXT_2};")
    stats_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
    top.addWidget(label_lbl, 1)
    top.addWidget(stats_lbl, 1)
    vbox.addLayout(top)
    glw = pg.GraphicsLayoutWidget()
    vbox.addWidget(glw, 1)
    help_lbl = QtWidgets.QLabel("keys: 0-9 label  |  space pause  |  a PC1/axes  |  d flex detrend  |  q quit     "
                                + "   ".join(f"{k} {v}" for k, v in LABELS.items()))
    help_lbl.setStyleSheet(f"color: {TEXT_2}; font-size: 10px;")
    vbox.addWidget(help_lbl)
    win.resize(1280, 860)
    win.show()

    plots: dict[str, object] = {}
    curves: dict[str, list] = {}
    label_img = pg.ImageItem()
    label_img.setLookupTable(build_label_lut())

    def make_plot(row: int, title: str, units: str, height: int | None = None):
        p = glw.addPlot(row=row, col=0)
        if title:
            p.setTitle(title, color=TEXT, size="11pt")
        p.setLabel("left", units)
        p.getAxis("left").enableAutoSIPrefix(False)
        p.getAxis("left").setWidth(60)
        p.showGrid(x=True, y=True, alpha=GRID_ALPHA)
        p.setXRange(-args.window, 0, padding=0)
        p.enableAutoRange(axis="y")
        p.setAutoVisible(y=True)
        p.setMouseEnabled(x=False, y=True)
        if height:
            p.setMaximumHeight(height)
        return p

    def build_ui(proc: LiveProcessor) -> None:
        row = 0
        for key, title in (("chest", "Chest respiration"), ("abd", "Abdomen respiration")):
            if key in proc.groups:
                p = make_plot(row, f"{title}  (0.05-1 Hz tilt)", "deg")
                plots[key] = p
                curves[key] = [p.plot(pen=pg.mkPen(SERIES[key], width=2))] + [
                    p.plot(pen=pg.mkPen(c, width=1)) for c in ("#3987e5", "#d95926", "#199e70")
                ]
                row += 1
        if proc.has_flex:
            p = make_plot(row, "Neck flex", "counts")
            plots["flex"] = p
            curves["flex"] = [p.plot(pen=pg.mkPen(SERIES["flex"], width=2))]
            row += 1
        if "chest" in proc.groups:
            p = make_plot(row, "Chest motion energy  (>1.5 Hz, rms)", "mg", 150)
            plots["motion"] = p
            curves["motion"] = [p.plot(pen=pg.mkPen(SERIES["motion"], width=2))]
            row += 1
        p = make_plot(row, "", "label", 80)
        p.getAxis("left").setTicks([[]])
        p.setMouseEnabled(x=False, y=False)
        p.addItem(label_img)
        p.setYRange(0, 1, padding=0)
        p.setLabel("bottom", "seconds")
        plots["label"] = p
        for k, pl in plots.items():
            if k != "label":
                pl.setXLink(plots["label"])

    rate_hist: list[tuple[float, int]] = []

    def tick() -> None:
        if state["error"]:  # stay on the error until the viewer is restarted
            return
        try:
            data = source.poll()
            if data:
                dec.feed(data)
        except LayoutChanged as e:
            state["error"] = f"layout changed, restart the viewer: {e}"
        except Exception as e:  # e.g. the serial port vanished in --port mode
            state["error"] = f"source error, restart the viewer: {e}"
        if state["error"]:
            label_lbl.setText(state["error"][:300])
            return
        if dec.header is not None and state["proc"] is None:
            state["proc"] = LiveProcessor(dec.header, args.window, args.hp)
            build_ui(state["proc"])
        proc: LiveProcessor | None = state["proc"]
        b = dec.drain()
        if proc is None:
            return
        if b is not None:
            proc.process(b)

        now = time.monotonic()
        rate_hist.append((now, dec.stats.frames_data))
        while rate_hist and now - rate_hist[0][0] > 3.0:
            rate_hist.pop(0)
        rate = (rate_hist[-1][1] - rate_hist[0][1]) / max(1e-6, rate_hist[-1][0] - rate_hist[0][0])
        lab = dec.label
        col = LABEL_COLORS_DARK.get(lab, TEXT_2)
        label_lbl.setText(f'<span style="color:{col}">&#9632;</span> {lab}  {label_name(lab)}'
                          + ("   [PAUSED]" if state["paused"] else ""))
        dev = dec.status
        devtxt = f" | dev ovr {dev.overruns} txdrop {dev.tx_drops}" if dev else ""
        stats_lbl.setText(f"{source.name}<br>{rate:5.1f} Hz | frames {dec.stats.frames_data} | lost "
                          f"{dec.stats.dropped} ({dec.stats.drop_pct():.3f}%) | crc {dec.parser.stats.crc_errors}{devtxt}")

        if state["paused"] or proc.t.count == 0:
            return
        t = proc.t.ordered()[:, 0]
        x = t - t[-1]
        for key in proc.groups:
            cs = curves[key]
            if state["axes"]:
                ax = proc.resp_axes_deg(key)
                cs[0].setData([], [])
                for i in range(3):
                    cs[1 + i].setData(x, ax[:, i])
            else:
                cs[0].setData(x, proc.resp_pc1_deg(key))
                for i in range(3):
                    cs[1 + i].setData([], [])
        if proc.has_flex:
            y = proc.flex_detr.ordered()[:, 0] if state["detrend"] else proc.flex.ordered()[:, 0]
            curves["flex"][0].setData(x, y)
            plots["flex"].setTitle("Neck flex " + ("(minus 10 s baseline)" if state["detrend"] else "(raw)"),
                                   color=TEXT, size="11pt")
        if "motion" in curves:
            curves["motion"][0].setData(x, proc.motion.ordered()[:, 0])
        labs = proc.label.ordered()[:, 0].astype(np.uint8)
        label_img.setImage(labs[:, None], autoLevels=False, levels=(0, 255))
        label_img.setRect(QtCore.QRectF(float(x[0]), 0.0, float(x[-1] - x[0]) or 1e-3, 1.0))

    timer = QtCore.QTimer()
    timer.timeout.connect(tick)
    timer.start(33)

    if args.exit_after:
        QtCore.QTimer.singleShot(int(args.exit_after * 1000), win.close)

    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
