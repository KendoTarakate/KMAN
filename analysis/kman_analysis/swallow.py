"""Swallow / neck-tension events from the neck flex channel.

1. Baseline = long centred moving median (cancels drift and posture offsets).
2. sigma_cal = robust std (1.4826 x MAD) of the deviation over the quiet
   calibration segment.
3. Events by hysteresis: start above k_on x sigma, end below k_off x sigma;
   short gaps merged; spikes shorter than min_duration dropped.
4. Classification. A swallow is a short, small deflection; head
   flexion/rotation bends the sensor far more and for longer. So an event is
   "head_motion" if its peak exceeds head_amplitude_k x sigma, it lasts longer
   than max_duration, or (with a neck IMU) the head tilts/turns past the
   configured angles during it. Everything else is "swallow".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import signal

from .preprocess import Uniform, runs


@dataclass
class NeckEvents:
    t: np.ndarray
    baseline: np.ndarray
    deviation: np.ndarray  # counts
    sigma_cal: float
    events: pd.DataFrame  # start_s, end_s, peak_s, duration_s, peak_dev, peak_k, kind, reason
    swallow_rate_10s: np.ndarray  # rolling count of swallow events per rate window
    head_rate_10s: np.ndarray


def _robust_std(x: np.ndarray) -> float:
    if len(x) == 0:
        return float("nan")
    return 1.4826 * float(np.median(np.abs(x - np.median(x))))


def head_pose_change(u: Uniform, a: int, b: int) -> tuple[float, float]:
    """(tilt change deg, yaw change deg) of the neck IMU over [a, b)."""
    acc = u.vec("neck", "a")
    if acc is None:
        return 0.0, 0.0
    sos = signal.butter(2, 2.0, btype="lowpass", fs=u.fs, output="sos")
    seg = signal.sosfiltfilt(sos, acc[a:b], axis=0) if b - a > 15 else acc[a:b]
    g = seg / np.linalg.norm(seg, axis=1, keepdims=True)
    ref = g[: max(1, int(0.2 * u.fs))].mean(axis=0)
    ref /= np.linalg.norm(ref)
    tilt = float(np.degrees(np.max(np.arccos(np.clip(g @ ref, -1, 1)))))
    yaw = 0.0
    gyr = u.vec("neck", "g")
    if gyr is not None:
        w_vert = np.sum(gyr[a:b] * g, axis=1)  # rate about the gravity axis, dps
        w_vert = w_vert - np.median(w_vert)
        yaw = float(np.max(np.abs(np.cumsum(w_vert) / u.fs)))
    return tilt, yaw


def analyze_neck(u: Uniform, cfg: dict, calib: np.ndarray) -> NeckEvents | None:
    if "flex_raw" not in u.ch:
        return None
    c = cfg["swallow"]
    x = u.ch["flex_raw"]
    win = max(3, int(c["baseline_window_s"] * u.fs) | 1)
    base = pd.Series(x).rolling(win, center=True, min_periods=1).median().to_numpy()
    dev = x - base
    sigma = max(c["sigma_floor_counts"], _robust_std(dev[calib]) if calib.any() else _robust_std(dev))

    if c["polarity"] == "positive":
        mag = dev
    elif c["polarity"] == "negative":
        mag = -dev
    else:
        mag = np.abs(dev)

    on = mag > c["k_on"] * sigma
    above_off = mag > c["k_off"] * sigma
    # hysteresis: grow each "on" run outwards while above k_off
    cand = []
    for a, b in runs(above_off):
        if on[a:b].any():
            cand.append([a, b])
    merged: list[list[int]] = []
    gap = int(c["merge_gap_s"] * u.fs)
    for a, b in cand:
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])

    hcfg = c["head_imu"]
    has_neck_imu = u.vec("neck", "a") is not None
    margin = int(hcfg["margin_s"] * u.fs)
    rows = []
    for a, b in merged:
        dur = (b - a) / u.fs
        if dur < c["min_duration_s"] or u.gap[a:b].any():
            continue
        k = a + int(np.argmax(mag[a:b]))
        peak_k = mag[k] / sigma
        kind, reason = "swallow", ""
        if peak_k > c["head_amplitude_k"]:
            kind, reason = "head_motion", "amplitude"
        elif dur > c["max_duration_s"]:
            kind, reason = "head_motion", "duration"
        if has_neck_imu:
            tilt, yaw = head_pose_change(u, max(0, a - margin), min(u.n, b + margin))
            if tilt > hcfg["tilt_change_deg"] or yaw > hcfg["yaw_change_deg"]:
                kind, reason = "head_motion", f"neck imu (tilt {tilt:.0f} deg, yaw {yaw:.0f} deg)"
        rows.append({"start_s": a / u.fs, "end_s": b / u.fs, "peak_s": k / u.fs, "duration_s": dur,
                     "peak_dev": float(dev[k]), "peak_k": float(peak_k), "kind": kind, "reason": reason,
                     "label": int(u.label[k])})
    ev = pd.DataFrame(rows, columns=["start_s", "end_s", "peak_s", "duration_s", "peak_dev", "peak_k", "kind",
                                     "reason", "label"])

    rw = int(c["rate_window_s"] * u.fs)

    def rolling_count(kind: str) -> np.ndarray:
        imp = np.zeros(u.n)
        for s in ev.loc[ev["kind"] == kind, "peak_s"]:
            imp[min(u.n - 1, int(round(s * u.fs)))] += 1
        return pd.Series(imp).rolling(rw, center=True, min_periods=1).sum().to_numpy()

    return NeckEvents(t=u.t, baseline=base, deviation=dev, sigma_cal=float(sigma), events=ev,
                      swallow_rate_10s=rolling_count("swallow"), head_rate_10s=rolling_count("head_motion"))
