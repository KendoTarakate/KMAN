"""Synthetic device: physiologically plausible signals in the exact firmware
frame format. Used to test the logger, live view and analysis without
hardware, and as ground truth for the analysis tests.

The models are deliberately simple but keep the properties the analysis
depends on: breathing is a slow chest-tilt oscillation with I:E asymmetry,
effort continues during obstruction (abdomen in anti-phase), swallows are
short small flex bumps with a brief swallow apnea, head movements are large
slow flex deflections, walking/coughing add energy above 1.5 Hz.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from . import protocol as P
from .labels import label_name

ACCEL_SCALE = 61e-6  # g/LSB at +-2 g
GYRO_SCALE = 8.75e-3  # dps/LSB at 250 dps
ADC_FULL_SCALE = 16383

DEFAULT_SCENARIO: list[tuple[int, float]] = [
    (1, 60), (8, 20), (1, 30), (4, 40), (1, 20), (5, 40), (1, 20), (3, 40),
    (2, 30), (6, 30), (1, 20), (7, 15), (1, 20), (9, 25), (1, 30),
]


@dataclass
class SimConfig:
    fs: int = 100
    seed: int = 1
    abdomen: bool = False
    neck_imu: bool = False
    resp_rate_bpm: float = 14.0
    resp_tilt_deg_pp: float = 1.0  # chest tilt peak-to-peak per breath at rest
    flex_baseline: float = 8200.0
    flex_noise: float = 6.0  # counts rms
    swallow_counts: float = 70.0  # flex bump amplitude of a swallow
    head_counts: tuple[float, float] = (400.0, 1200.0)
    accel_noise_g: float = 0.35e-3
    gyro_noise_dps: float = 0.03
    gyro_bias_dps: tuple[float, float, float] = (0.4, -0.25, 0.15)
    t0_us: int = 1_000_000  # set near 2**32 to exercise timestamp wrap
    seq0: int = 100


@dataclass
class SimSession:
    cfg: SimConfig
    labels: np.ndarray  # per-sample label
    raw: dict[str, np.ndarray]  # channel name -> int16
    header: dict
    truth: dict = field(default_factory=dict)  # ground truth for tests

    @property
    def n(self) -> int:
        return len(self.labels)

    def label_events(self) -> list[dict]:
        """Label changes as if the operator pressed the keys at segment boundaries."""
        ev = []
        prev = None
        for i in np.flatnonzero(np.r_[True, self.labels[1:] != self.labels[:-1]]):
            lab = int(self.labels[i])
            if lab != prev:
                ev.append({"seq": self.cfg.seq0 + int(i), "t_us": self.cfg.t0_us + int(i) * self.period_us,
                           "host_time": 0.0, "label": lab, "name": label_name(lab)})
                prev = lab
        return ev

    @property
    def period_us(self) -> int:
        return 1_000_000 // self.cfg.fs

    def header_frame(self, i: int = -1) -> bytes:
        """Header as the firmware sends it at sample index i (its timestamp is the current device time)."""
        hdr = dict(self.header, t_us_at_header=(self.cfg.t0_us + max(i, 0) * self.period_us) & 0xFFFFFFFF)
        return P.encode_frame(P.TYPE_HEADER, json.dumps(hdr).encode())

    def data_frame(self, i: int) -> bytes:
        t32 = (self.cfg.t0_us + i * self.period_us) & 0xFFFFFFFF
        seq16 = (self.cfg.seq0 + i) & 0xFFFF
        body = P.DATA_PREFIX.pack(t32, seq16, self.header["present_mask"])
        body += np.array([self.raw[c["name"]][i] for c in self.header["channels"]], dtype="<i2").tobytes()
        return P.encode_frame(P.TYPE_DATA, body)

    def status_frame(self, i: int, sent: int, drops: int = 0) -> bytes:
        st = P.DeviceStatus(
            t_us=(self.cfg.t0_us + i * self.period_us) & 0xFFFFFFFF, ticks=self.cfg.seq0 + i, frames_sent=sent,
            overruns=0, tx_drops=drops, read_errors=(0, 0, 0, 0), max_latency_us=620, tx_high_water=180,
            flags=P.STATUS_FLAG_HFXO,
        )
        return P.encode_frame(P.TYPE_STATUS, P.encode_status(st))

    def stream(self, drop_prob: float = 0.0, corrupt_prob: float = 0.0, seed: int = 0, status_every_s: float = 1.0):
        """Yields (sample_index, frame_bytes) in device order, header first."""
        rng = np.random.default_rng(seed)
        yield -1, self.header_frame()
        every = int(status_every_s * self.cfg.fs)
        sent = 0
        for i in range(self.n):
            if drop_prob and rng.random() < drop_prob:
                continue
            fr = self.data_frame(i)
            if corrupt_prob and rng.random() < corrupt_prob:
                b = bytearray(fr)
                b[rng.integers(5, len(b))] ^= 0xFF
                fr = bytes(b)
            sent += 1
            yield i, fr
            if every and i % every == every - 1:
                yield i, self.status_frame(i, sent)


# --------------------------------------------------------------------------- signal models


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    k = np.ones(n) / n
    # Reflect, not edge: edge padding would repeat one noise sample n/2 times and
    # make smoothed noise swing far more near the ends than in the interior.
    pad = np.pad(x, (n // 2, n - 1 - n // 2), mode="reflect" if len(x) > n else "edge")
    return np.convolve(pad, k, mode="valid")


def _rot_x(v: np.ndarray, a: np.ndarray) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.stack([v[:, 0], c * v[:, 1] - s * v[:, 2], s * v[:, 1] + c * v[:, 2]], axis=1)


def _rot_z(v: np.ndarray, a: np.ndarray) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.stack([c * v[:, 0] - s * v[:, 1], s * v[:, 0] + c * v[:, 1], v[:, 2]], axis=1)


def _bandnoise(rng, n, fs, lo, hi, rms):
    x = rng.standard_normal(n)
    f = np.fft.rfftfreq(n, 1 / fs)
    X = np.fft.rfft(x)
    X[(f < lo) | (f > hi)] = 0
    y = np.fft.irfft(X, n)
    s = y.std()
    return y * (rms / s) if s > 0 else y


def _bump(n_total: int, start: int, dur: int, amp: float, shape: str = "cos") -> tuple[slice, np.ndarray]:
    end = min(n_total, start + dur)
    t = np.arange(end - start) / max(dur, 1)
    if shape == "plateau":  # head movement: ramp up, hold, ramp down
        y = np.clip(np.minimum(t / 0.25, (1 - t) / 0.25), 0, 1)
        y = 0.5 - 0.5 * np.cos(np.pi * y)
    else:
        y = 0.5 - 0.5 * np.cos(2 * np.pi * t)
    return slice(start, end), amp * y


def _resp_shape(phase_frac: np.ndarray, ie_split: float = 0.38) -> np.ndarray:
    """One breath per unit phase: fast inspiration (rise), slower expiration. Range -0.5..0.5."""
    p = phase_frac % 1.0
    insp = p < ie_split
    r = np.where(insp, 0.5 - 0.5 * np.cos(np.pi * p / ie_split), 0.5 + 0.5 * np.cos(np.pi * (p - ie_split) / (1 - ie_split)))
    return r - 0.5


def generate(cfg: SimConfig | None = None, scenario: list[tuple[int, float]] | None = None) -> SimSession:
    cfg = cfg or SimConfig()
    scenario = scenario or DEFAULT_SCENARIO
    rng = np.random.default_rng(cfg.seed)
    fs = cfg.fs
    labels = np.concatenate([np.full(int(d * fs), lab, dtype=np.int8) for lab, d in scenario])
    n = len(labels)
    t = np.arange(n) / fs

    def is_(lab):
        return (labels == lab).astype(float)

    # ---- respiration: rate, amplitude envelope, phase
    rate = cfg.resp_rate_bpm * (1 + 0.08 * _smooth(rng.standard_normal(n), 10 * fs) * 8)
    rate = rate * (1 + 0.5 * is_(6)) * (1 + 0.45 * is_(9)) * (1 + 0.15 * is_(2))
    phase = np.cumsum(rate / 60.0) / fs
    amp = 1.0 + 0.3 * is_(6) + 0.6 * is_(9) - 0.25 * is_(2) - 1.0 * is_(8)
    breath_idx = np.floor(phase).astype(int)
    per_breath = 1 + 0.12 * rng.standard_normal(breath_idx.max() + 2)
    amp = amp * per_breath[breath_idx]
    talk_mod = 1 + 0.5 * _smooth(rng.standard_normal(n), 2 * fs) * 4
    amp = np.where(labels == 2, amp * np.clip(talk_mod, 0.3, 1.6), amp)
    amp = np.clip(_smooth(np.clip(amp, 0, None), fs), 0, None)

    flex_delta = np.zeros(n)
    head_pitch = np.zeros(n)  # neck IMU, rad
    chest_jolt = np.zeros((n, 3))
    gyro_jolt = np.zeros((n, 3))
    swallow_times: list[float] = []
    head_times: list[float] = []

    # ---- swallows (dry swallow every ~8 s; drinking: sips with 1-2 swallows + mild head tilt)
    apnea = np.ones(n)
    for lab, gap_s in ((4, 8.0), (3, 10.0)):
        idx = np.flatnonzero(labels == lab)
        if not len(idx):
            continue
        segs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        for seg in segs:
            k = seg[0] + int(3 * fs)
            while k < seg[-1] - 3 * fs:
                count = 1 if lab == 4 else int(rng.integers(1, 3))
                if lab == 3:  # cup to lips: head extension ~ 2.5 s, moderate flex change
                    sl, y = _bump(n, k - int(1.0 * fs), int(3.5 * fs), -180.0, "plateau")
                    flex_delta[sl] += y
                    head_pitch[sl] += np.deg2rad(-15) * y / -180.0
                for j in range(count):
                    ks = k + int(j * 1.6 * fs)
                    dur = int(rng.uniform(0.6, 1.2) * fs)
                    sl, y = _bump(n, ks, dur, cfg.swallow_counts * rng.uniform(0.8, 1.25))
                    flex_delta[sl] += y
                    swallow_times.append(ks / fs)
                    # swallow apnea ~1 s
                    a0, a1 = max(0, ks - int(0.3 * fs)), min(n, ks + int(0.9 * fs))
                    apnea[a0:a1] = 0.15
                k += int(gap_s * fs * rng.uniform(0.8, 1.2))
    amp = amp * _smooth(apnea, int(0.3 * fs))

    # ---- head movements: large slow flex deflections
    idx = np.flatnonzero(labels == 5)
    if len(idx):
        segs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        for seg in segs:
            k = seg[0] + int(2 * fs)
            while k < seg[-1] - 4 * fs:
                dur = int(rng.uniform(1.5, 3.0) * fs)
                a = rng.uniform(*cfg.head_counts) * rng.choice([-1, 1])
                sl, y = _bump(n, k, dur, a, "plateau")
                flex_delta[sl] += y
                head_pitch[sl] += np.deg2rad(35) * y / cfg.head_counts[1]
                chest_jolt[sl, 1] += 0.004 * y / abs(a)  # small shoulder coupling
                head_times.append(k / fs)
                k += int(rng.uniform(3.5, 5.0) * fs)

    # ---- coughs
    idx = np.flatnonzero(labels == 7)
    if len(idx):
        segs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        for seg in segs:
            k = seg[0] + int(1.5 * fs)
            while k < seg[-1] - fs:
                m = min(n, k + int(0.3 * fs))
                tt = np.arange(m - k) / fs
                env = np.exp(-tt / 0.06)
                chest_jolt[k:m, 2] += 0.35 * env * np.sin(2 * np.pi * 14 * tt)
                chest_jolt[k:m, 1] += 0.12 * env
                gyro_jolt[k:m, 0] += 25 * env * np.sin(2 * np.pi * 9 * tt)
                sl, y = _bump(n, k, int(0.35 * fs), 250.0)
                flex_delta[sl] += y
                k += int(rng.uniform(2.5, 4.5) * fs)

    resp = amp * _resp_shape(phase)  # -0.5..0.5 per unit amplitude
    tilt_amp = np.deg2rad(cfg.resp_tilt_deg_pp)
    th_x = tilt_amp * resp
    th_z = 0.25 * tilt_amp * resp
    # tiny cardiac ballistic component, visible during breath-hold
    th_x = th_x + np.deg2rad(0.012) * np.sin(2 * np.pi * 1.15 * t)
    # slow postural sway
    th_x = th_x + np.deg2rad(0.3) * _smooth(rng.standard_normal(n), 20 * fs) * 10

    # ---- walking
    walk = _smooth(is_(6), fs)
    acc_walk = np.stack([
        0.10 * np.sin(2 * np.pi * 0.9 * t),
        0.25 * np.sin(2 * np.pi * 1.8 * t) + 0.08 * np.sin(2 * np.pi * 3.6 * t + 0.4),
        0.06 * np.sin(2 * np.pi * 1.8 * t + 1.0),
    ], axis=1) * walk[:, None]
    gyr_walk = np.stack([
        5.0 * np.sin(2 * np.pi * 1.8 * t),
        3.0 * np.sin(2 * np.pi * 0.9 * t + 0.3),
        8.0 * np.sin(2 * np.pi * 0.9 * t),
    ], axis=1) * walk[:, None]

    # ---- talking: chest/laryngeal vibration
    talk = _smooth(is_(2), fs)
    acc_talk = np.stack([_bandnoise(rng, n, fs, 5, 15, 0.004) for _ in range(3)], axis=1) * talk[:, None]
    flex_delta += _bandnoise(rng, n, fs, 0.8, 4.0, 12.0) * talk

    # ---- chest IMU: gravity rotated by -tilt (body rotation +tilt), see analysis complementary filter
    up = np.array([0.0, 0.97, 0.243])
    up = up / np.linalg.norm(up)
    g = np.repeat(up[None, :], n, axis=0)
    g = _rot_z(_rot_x(g, -th_x), -th_z)
    acc = g + acc_walk + acc_talk + chest_jolt
    acc += rng.standard_normal((n, 3)) * cfg.accel_noise_g
    gyr = np.stack([np.gradient(th_x) * fs, np.zeros(n), np.gradient(th_z) * fs], axis=1) * 180 / np.pi
    gyr += gyr_walk + gyro_jolt + np.array(cfg.gyro_bias_dps)
    gyr += rng.standard_normal((n, 3)) * cfg.gyro_noise_dps

    raw: dict[str, np.ndarray] = {}
    for i, ax in enumerate("xyz"):
        raw[f"chest_a{ax}"] = np.clip(np.round(acc[:, i] / ACCEL_SCALE), -32768, 32767).astype(np.int16)
    for i, ax in enumerate("xyz"):
        raw[f"chest_g{ax}"] = np.clip(np.round(gyr[:, i] / GYRO_SCALE), -32768, 32767).astype(np.int16)

    truth = {"resp": resp, "swallow_times": np.array(swallow_times), "head_times": np.array(head_times),
             "rate_bpm": rate, "amp": amp}

    if cfg.abdomen:
        # Normal: abdomen leads slightly, larger excursion. Obstruction: anti-phase (paradox).
        paradox = _smooth(is_(9), fs)
        resp_ab = amp * _resp_shape(phase + 0.03) * 1.2 * (1 - 2 * paradox)
        th_ab = tilt_amp * 1.3 * resp_ab
        up_ab = np.array([0.0, 0.9, 0.436])
        up_ab = up_ab / np.linalg.norm(up_ab)
        ga = _rot_x(np.repeat(up_ab[None, :], n, axis=0), -th_ab)
        acc_ab = ga + acc_walk * 1.1 + chest_jolt * 0.5 + rng.standard_normal((n, 3)) * cfg.accel_noise_g
        gyr_ab = np.stack([np.gradient(th_ab) * fs * 180 / np.pi, np.zeros(n), np.zeros(n)], axis=1)
        gyr_ab += gyr_walk * 0.8 + np.array([-0.2, 0.3, 0.1]) + rng.standard_normal((n, 3)) * cfg.gyro_noise_dps
        for i, ax in enumerate("xyz"):
            raw[f"abd_a{ax}"] = np.clip(np.round(acc_ab[:, i] / ACCEL_SCALE), -32768, 32767).astype(np.int16)
        for i, ax in enumerate("xyz"):
            raw[f"abd_g{ax}"] = np.clip(np.round(gyr_ab[:, i] / GYRO_SCALE), -32768, 32767).astype(np.int16)
        truth["resp_abd"] = resp_ab

    drift = 50 * np.sin(2 * np.pi * t / 900.0)
    pulse = 3.0 * np.sin(2 * np.pi * 1.15 * t)
    flex = cfg.flex_baseline + drift + pulse + flex_delta + rng.standard_normal(n) * cfg.flex_noise
    raw["flex_raw"] = np.clip(np.round(flex), 0, ADC_FULL_SCALE).astype(np.int16)

    if cfg.neck_imu:
        hp = head_pitch + np.deg2rad(0.2) * np.sin(2 * np.pi * 0.25 * t)
        up_n = np.array([0.0, 1.0, 0.0])
        gn = _rot_x(np.repeat(up_n[None, :], n, axis=0), -hp)
        acc_n = gn + acc_walk + rng.standard_normal((n, 3)) * cfg.accel_noise_g
        gyr_n = np.stack([np.gradient(hp) * fs * 180 / np.pi, np.zeros(n), np.zeros(n)], axis=1)
        gyr_n += rng.standard_normal((n, 3)) * cfg.gyro_noise_dps
        for i, ax in enumerate("xyz"):
            raw[f"neck_a{ax}"] = np.clip(np.round(acc_n[:, i] / ACCEL_SCALE), -32768, 32767).astype(np.int16)
        for i, ax in enumerate("xyz"):
            raw[f"neck_g{ax}"] = np.clip(np.round(gyr_n[:, i] / GYRO_SCALE), -32768, 32767).astype(np.int16)

    header = build_header(cfg)
    # Order raw channels as announced in the header.
    raw = {c["name"]: raw[c["name"]] for c in header["channels"]}
    return SimSession(cfg=cfg, labels=labels, raw=raw, header=header, truth=truth)


def build_header(cfg: SimConfig, stream_id: int = 1) -> dict:
    """Session header in the firmware's format (see firmware/src/main.cpp buildHeaderFrame)."""

    def imu(slot, prefix, bit, addr):
        sensor = {"slot": slot, "bit": bit, "status": "ok", "model": "LSM6DSOX", "bus": "i2c", "addr": addr,
                  "mode": "timer-master register read (no FIFO)", "odr_hz": 208, "accel_range_g": 2,
                  "accel_lpf": "LPF2 ODR/10", "accel_lpf_hz": 20.8, "gyro_range_dps": 250,
                  "gyro_lpf": "LPF1 FTYPE=6 (~26 Hz at 208/416 Hz ODR)", "block_data_update": True}
        chans = [{"name": f"{prefix}_{s}", "slot": slot, "bit": bit, "unit": "g" if s[0] == "a" else "dps",
                  "scale": ACCEL_SCALE if s[0] == "a" else GYRO_SCALE} for s in ("ax", "ay", "az", "gx", "gy", "gz")]
        return sensor, chans

    sensors, channels = [], []
    s, c = imu("chest_imu", "chest", 0, "0x6A")
    sensors.append(s)
    channels += c
    if cfg.abdomen:
        s, c = imu("abdomen_imu", "abd", 1, "0x6B")
        sensors.append(s)
        channels += c
    else:
        sensors.append({"slot": "abdomen_imu", "bit": 1, "status": "not_configured"})
    sensors.append({"slot": "neck_flex", "bit": 2, "status": "ok", "model": "resistive divider",
                    "sensor": "simulated flex", "r_fixed_ohm": 47000, "topology": "3V3 - flex - ADC - R_fixed - GND",
                    "pin": 0, "adc": "simulated", "bits": 14, "full_scale": ADC_FULL_SCALE})
    channels.append({"name": "flex_raw", "slot": "neck_flex", "bit": 2, "unit": "counts", "scale": 1})
    if cfg.neck_imu:
        s, c = imu("neck_imu", "neck", 3, "0x6B")
        sensors.append(s)
        channels += c
    else:
        sensors.append({"slot": "neck_imu", "bit": 3, "status": "not_configured"})
    present = sum(1 << x["bit"] for x in sensors if x["status"] == "ok")
    return {
        "type": "kman_session_header", "protocol": 1, "fw_version": "sim", "build": "simulator",
        "board": "simulator", "mcu": "none", "device_id": "SIMULATOR0000000", "boot_id": "0x00000001",
        "stream_id": stream_id,
        "t_us_at_header": cfg.t0_us, "sample_rate_hz": cfg.fs, "period_us": 1_000_000 // cfg.fs,
        "clock": {"source": "simulated", "hfclk": "simulated", "t_unit": "us", "t_bits": 32, "seq_bits": 16},
        "frame": {"sync": "AA55", "len": "u16le, bytes of type+body",
                  "crc": "CRC-16/CCITT-FALSE over len,type,body; u16le",
                  "data_prefix": ["t_us:u32", "seq:u16", "valid:u8"], "channel_type": "i16"},
        "present_mask": present, "sensors": sensors, "channels": channels,
    }
