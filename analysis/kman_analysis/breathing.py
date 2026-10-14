"""Respiratory waveform and breath detection from a chest/abdomen IMU.

Pipeline (spec section "Breathing extraction"):

1. Orientation: complementary filter on the gravity direction in the sensor
   frame. Gyro integration supplies content above ~1/(2*pi*tau) Hz, the
   normalised accelerometer the rest. Tracking the gravity *vector* (not Euler
   angles) has no singularity in any posture. During walking the gyro path is
   what keeps step accelerations out of the tilt estimate.
2. Decimate to resp_fs and band-pass 0.05-1 Hz with zero-phase filtfilt.
3. Windowed PCA (Hann overlap-add, sign kept continuous between windows)
   reduces the 3-D tilt to one waveform in degrees. Its global sign is set so
   that inspiration (the faster phase) rises; peaks are then end-inspiration.
4. Peaks with adaptive prominence: max(abs floor, rel x rolling reference
   peak-to-peak amplitude). Breaths during motion artifacts or data gaps are
   kept but marked invalid; "seconds since last valid breath" uses valid ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import signal

from .preprocess import Uniform


@dataclass
class Breathing:
    fs: float  # waveform rate (resp_fs)
    t: np.ndarray
    wave_deg: np.ndarray
    ref_pp_deg: np.ndarray  # reference peak-to-peak amplitude used for the threshold
    breath_t: np.ndarray  # seconds
    breath_amp_deg: np.ndarray
    breath_valid: np.ndarray  # bool
    rate_t: np.ndarray  # time of each rate value (second breath of the interval)
    rate_bpm: np.ndarray
    since_last_s: np.ndarray  # on the full-rate grid
    sign_flipped: bool


def gravity_complementary(acc_g: np.ndarray, gyro_dps: np.ndarray, fs: float, tau_s: float) -> np.ndarray:
    """Unit gravity-direction vector per sample (sensor frame).

    Prediction: dg/dt = -omega x g (gravity seen from the rotating body).
    Correction: blend with the normalised accelerometer, weight dt/(tau+dt).
    """
    n = len(acc_g)
    dt = 1.0 / fs
    alpha = tau_s / (tau_s + dt)
    beta = 1.0 - alpha
    norm = np.linalg.norm(acc_g, axis=1)
    norm[norm == 0] = 1.0
    a = (acc_g / norm[:, None]).tolist()
    w = (np.radians(gyro_dps) * dt).tolist()
    out = np.empty((n, 3))
    gx, gy, gz = a[0]
    for k in range(n):
        wx, wy, wz = w[k]
        # g_pred = g - (w*dt) x g
        px = gx - (wy * gz - wz * gy)
        py = gy - (wz * gx - wx * gz)
        pz = gz - (wx * gy - wy * gx)
        ax, ay, az = a[k]
        gx = alpha * px + beta * ax
        gy = alpha * py + beta * ay
        gz = alpha * pz + beta * az
        inv = 1.0 / (gx * gx + gy * gy + gz * gz) ** 0.5
        gx *= inv
        gy *= inv
        gz *= inv
        out[k] = (gx, gy, gz)
    return out


def bandpass(x: np.ndarray, fs: float, band: tuple[float, float], order: int) -> np.ndarray:
    """Zero-phase band-pass. A 0.05 Hz corner rings for ~20 s, so the edges are
    padded by 3 corner periods (even extension, median removed); the default
    short pad leaves ~30 s of edge transient that swamps the breathing."""
    sos = signal.butter(order, band, btype="bandpass", fs=fs, output="sos")
    x = x - np.median(x, axis=0)
    padlen = min(len(x) - 1, int(3 * fs / band[0]))
    return signal.sosfiltfilt(sos, x, axis=0, padtype="even", padlen=padlen)


def windowed_pca(x: np.ndarray, fs: float, window_s: float, weight: np.ndarray | None = None) -> np.ndarray:
    """Project (n, d) onto a slowly varying first principal axis.

    Each 50 %-overlapping window gets its own PC1 (covariance from samples with
    weight > 0 when enough exist), sign-aligned to the previous window, and the
    projections are blended with Hann weights.
    """
    n = len(x)
    L = max(8, int(window_s * fs))
    if n <= L:
        starts = [0]
        L = n
    else:
        hop = L // 2
        starts = list(range(0, n - L + 1, hop))
        if starts[-1] + L < n:
            starts.append(n - L)
    win = np.hanning(L + 2)[1:-1]
    y = np.zeros(n)
    wsum = np.zeros(n)
    prev = None
    for s in starts:
        seg = x[s : s + L]
        use = seg
        if weight is not None:
            good = weight[s : s + L] > 0
            if good.sum() >= 0.3 * L:
                use = seg[good]
        c = np.cov((use - use.mean(axis=0)).T)
        _, vecs = np.linalg.eigh(c)
        v = vecs[:, -1]
        if prev is not None and float(v @ prev) < 0:
            v = -v
        prev = v
        y[s : s + L] += win * ((seg - seg.mean(axis=0)) @ v)
        wsum[s : s + L] += win
    return y / np.maximum(wsum, 1e-9)


def motion_energy_mg(acc_g: np.ndarray, fs: float, cfg: dict) -> np.ndarray:
    m = cfg["motion"]
    sos = signal.butter(4, m["highpass_hz"], btype="highpass", fs=fs, output="sos")
    hp = signal.sosfiltfilt(sos, acc_g, axis=0)
    e = np.sum(hp**2, axis=1)
    win = max(1, int(m["window_s"] * fs))
    ms = pd.Series(e).rolling(win, center=True, min_periods=1).mean().to_numpy()
    return np.sqrt(ms) * 1000.0


def respiratory_waveform(u: Uniform, prefix: str, cfg: dict, calib: np.ndarray,
                         motion: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Returns (t, waveform_deg, resp_fs) for the IMU with channel prefix 'chest'/'abd'."""
    b = cfg["breathing"]
    acc = u.vec(prefix, "a")
    gyr = u.vec(prefix, "g")
    if gyr is not None:
        nb = int(b["gyro_bias_s"] * u.fs)
        cal_idx = np.flatnonzero(calib)[:nb]
        bias = np.median(gyr[cal_idx], axis=0) if len(cal_idx) else np.median(gyr[:nb], axis=0)
        g = gravity_complementary(acc, gyr - bias, u.fs, b["comp_filter_tau_s"])
    else:
        g = acc / np.linalg.norm(acc, axis=1, keepdims=True)

    q = max(1, int(round(u.fs / b["resp_fs_hz"])))
    rfs = u.fs / q
    gd = signal.decimate(g, q, ftype="fir", axis=0, zero_phase=True) if q > 1 else g
    tilt = bandpass(gd, rfs, tuple(b["band_hz"]), b["filter_order"])
    clean = (~motion[::q][: len(gd)]) & (~u.gap[::q][: len(gd)])
    wave = np.degrees(windowed_pca(tilt, rfs, b["pca_window_s"], weight=clean.astype(float)))
    t = np.arange(len(wave)) / rfs
    return t, wave, rfs


def orient_inspiration_up(wave: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, bool]:
    """Inspiration is shorter than expiration, so the rising slope is steeper:
    the derivative is positively skewed when inspiration points up."""
    d = np.diff(wave)[mask[1:]] if mask[1:].sum() > 50 else np.diff(wave)
    if len(d) < 10 or d.std() == 0:
        return wave, False
    skew = float(np.mean(((d - d.mean()) / d.std()) ** 3))
    return (-wave, True) if skew < 0 else (wave, False)


def detect_breaths(t: np.ndarray, wave: np.ndarray, fs: float, invalid: np.ndarray, cfg: dict,
                   n_full: int, fs_full: float) -> Breathing:
    p = cfg["breathing"]["peaks"]
    win = max(3, int(p["ref_window_s"] * fs))
    # Reference amplitude from clean samples only: walking sway or coughs must not
    # raise the threshold for the breathing around them.
    mag = np.where(invalid, np.nan, np.abs(wave))
    ref = pd.Series(mag).rolling(win, center=True, min_periods=win // 4).quantile(p["ref_quantile"])
    ref_pp = 2.0 * ref.bfill().ffill().to_numpy()
    prom_min = np.maximum(p["abs_min_prominence_deg"], p["rel_prominence"] * ref_pp)
    peaks, props = signal.find_peaks(wave, prominence=(prom_min, None), distance=max(1, int(p["min_interval_s"] * fs)))
    bt = t[peaks]
    amp = props["prominences"]
    valid = ~invalid[peaks] if len(peaks) else np.zeros(0, dtype=bool)

    # Rate from consecutive *detected* breaths, keeping only intervals with no artifact or
    # gap sample anywhere between them (ends included). A breath hidden by an artifact or
    # an interpolated gap would otherwise leave one double-length interval: half the rate.
    iv = np.diff(bt)
    ci = np.concatenate([[0], np.cumsum(invalid)])
    clean = (ci[peaks[1:] + 1] - ci[peaks[:-1]]) == 0
    keep = clean & (iv <= cfg["breathing"]["max_rate_interval_s"])
    rate_t = bt[1:][keep]
    rate = 60.0 / iv[keep]
    vt = bt[valid]

    tf = np.arange(n_full) / fs_full
    since = np.full(n_full, np.nan)
    if len(vt):
        k = np.searchsorted(vt, tf, side="right") - 1
        has = k >= 0
        since[has] = tf[has] - vt[k[has]]
    return Breathing(fs=fs, t=t, wave_deg=wave, ref_pp_deg=ref_pp, breath_t=bt, breath_amp_deg=amp,
                     breath_valid=valid, rate_t=rate_t, rate_bpm=rate, since_last_s=since, sign_flipped=False)


def analyze_breathing(u: Uniform, prefix: str, cfg: dict, calib: np.ndarray, motion: np.ndarray) -> Breathing:
    t, wave, rfs = respiratory_waveform(u, prefix, cfg, calib, motion)
    q = int(round(u.fs / rfs))
    cal_r = calib[::q][: len(wave)]
    wave, flipped = orient_inspiration_up(wave, cal_r)
    invalid = (motion[::q] | u.gap[::q])[: len(wave)]
    br = detect_breaths(t, wave, rfs, invalid, cfg, u.n, u.fs)
    br.sign_flipped = flipped
    return br
