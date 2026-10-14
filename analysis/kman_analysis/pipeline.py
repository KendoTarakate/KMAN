"""Run the full Step 1 analysis on a session and summarise it per label."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from kman_host.session_io import Session

from .breathing import Breathing, analyze_breathing, motion_energy_mg
from .phase import Phase, chest_abdomen_phase
from .preprocess import Uniform, calibration_mask, runs, to_uniform
from .swallow import NeckEvents, analyze_neck


@dataclass
class Analysis:
    session: Session
    cfg: dict
    u: Uniform
    calib: np.ndarray
    motion_mg: np.ndarray | None
    motion: np.ndarray
    chest: Breathing | None
    abd: Breathing | None
    phase: Phase | None
    neck: NeckEvents | None
    summary: pd.DataFrame
    qa: dict


def analyze(session: Session, cfg: dict) -> Analysis:
    u = to_uniform(session, cfg)
    calib = calibration_mask(u, cfg)

    acc = u.vec("chest", "a")
    if acc is not None:
        motion_mg = motion_energy_mg(acc, u.fs, cfg)
        motion = motion_mg > cfg["motion"]["threshold_mg"]
    else:
        motion_mg, motion = None, np.zeros(u.n, dtype=bool)

    chest = analyze_breathing(u, "chest", cfg, calib, motion) if acc is not None else None
    abd = analyze_breathing(u, "abd", cfg, calib, motion) if u.vec("abd", "a") is not None else None

    phase = None
    if chest is not None and abd is not None:
        q = int(round(u.fs / chest.fs))
        n = min(len(chest.wave_deg), len(abd.wave_deg))
        inv = (motion[::q] | u.gap[::q])[:n]
        phase = chest_abdomen_phase(chest.t[:n], chest.wave_deg[:n], abd.wave_deg[:n], chest.fs,
                                    calib[::q][:n], inv, cfg)

    neck = analyze_neck(u, cfg, calib)
    a = Analysis(session=session, cfg=cfg, u=u, calib=calib, motion_mg=motion_mg, motion=motion, chest=chest,
                 abd=abd, phase=phase, neck=neck, summary=pd.DataFrame(), qa={})
    a.summary = per_label_summary(a)
    a.qa = quality(a)
    return a


def _circmean_deg(x: np.ndarray) -> float:
    if len(x) == 0:
        return float("nan")
    return float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(x))))))


def _max_breath_gap(m: np.ndarray, bad: np.ndarray, valid_breaths_t: np.ndarray, fs: float) -> float:
    """Longest stretch without a valid breath inside this label's segments
    (segment edges count as boundaries; segments mostly under artifact are skipped)."""
    best = np.nan
    for a, b in runs(m):
        if bad[a:b].mean() > 0.5:
            continue
        t0, t1 = a / fs, b / fs
        pts = np.r_[t0, valid_breaths_t[(valid_breaths_t >= t0) & (valid_breaths_t < t1)], t1]
        g = float(np.max(np.diff(pts)))
        best = g if np.isnan(best) else max(best, g)
    return best


def per_label_summary(a: Analysis) -> pd.DataFrame:
    u, fs = a.u, a.u.fs
    rows = []
    for lab in sorted(np.unique(u.label)):
        m = u.label == lab
        dur = m.sum() / fs
        clean = m & ~a.motion & ~u.gap
        clean_min = clean.sum() / fs / 60.0
        r = {"label": int(lab), "name": u.label_names.get(int(lab), str(lab)), "duration_s": round(dur, 1),
             "artifact_pct": 100.0 * a.motion[m].mean(), "gap_pct": 100.0 * u.gap[m].mean()}
        if a.chest is not None:
            br = a.chest
            bi = np.clip(np.round(br.breath_t * fs).astype(int), 0, u.n - 1)
            in_lab = m[bi] & br.breath_valid
            r["breaths"] = int(in_lab.sum())
            r["breaths_per_min"] = in_lab.sum() / clean_min if clean_min > 10 / 60 else np.nan
            ri = np.clip(np.round(br.rate_t * fs).astype(int), 0, u.n - 1)
            rr = br.rate_bpm[m[ri]]
            r["rate_median_bpm"] = float(np.median(rr)) if len(rr) else np.nan
            amps = br.breath_amp_deg[in_lab]
            r["amp_median_deg"] = float(np.median(amps)) if len(amps) else np.nan
            r["max_breath_gap_s"] = _max_breath_gap(m, a.motion | u.gap, br.breath_t[br.breath_valid], fs)
        if a.phase is not None:
            q = int(round(fs / a.chest.fs))
            mp = m[::q][: len(a.phase.t)]
            v = mp & a.phase.valid
            r["phase_valid_pct"] = 100.0 * v.sum() / max(1, mp.sum())
            r["phase_mean_deg"] = _circmean_deg(a.phase.phase_deg[v])
            r["paradox_pct"] = 100.0 * (mp & a.phase.paradox).sum() / max(1, v.sum()) if v.any() else np.nan
        if a.neck is not None:
            ev = a.neck.events
            pi = np.clip(np.round(ev["peak_s"].to_numpy() * fs).astype(int), 0, u.n - 1) if len(ev) else np.zeros(0, int)
            in_lab = m[pi] if len(ev) else np.zeros(0, bool)
            kinds = ev["kind"].to_numpy() if len(ev) else np.zeros(0, object)
            n_sw = int((in_lab & (kinds == "swallow")).sum())
            n_hd = int((in_lab & (kinds == "head_motion")).sum())
            win = a.cfg["swallow"]["rate_window_s"]
            r["swallows"] = n_sw
            r["swallows_per_10s"] = n_sw / dur * win if dur else np.nan
            r["head_events"] = n_hd
            r["head_events_per_10s"] = n_hd / dur * win if dur else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


def quality(a: Analysis) -> dict:
    s, u = a.session, a.u
    st = s.stats.get("stream", {})
    dev = s.stats.get("device_status_last", {})
    qa = {
        "duration_s": round(u.n / u.fs, 1),
        "samples": int(len(s.df)),
        "lost_frames": int(st.get("dropped", u.n - len(s.df))),
        "lost_pct": round(100.0 * (u.n - len(s.df)) / max(1, u.n), 4),
        "crc_errors": int(s.stats.get("parser", {}).get("crc_errors", 0)),
        "segments": int(st.get("segments", 1)),
        "long_gap_pct": round(100.0 * u.gap.mean(), 3),
        "device_overruns": dev.get("overruns"),
        "device_tx_drops": dev.get("tx_drops"),
        "hf_crystal": dev.get("hfxo"),
        "calibration_s": round(a.calib.sum() / u.fs, 1),
        "artifact_pct": round(100.0 * a.motion.mean(), 2),
    }
    if a.neck is not None:
        qa["flex_sigma_cal_counts"] = round(a.neck.sigma_cal, 2)
    if a.chest is not None:
        qa["chest_sign_flipped"] = a.chest.sign_flipped
        acc = u.vec("chest", "a")
        qa["chest_accel_clipped_pct"] = round(100.0 * float(np.mean(np.abs(acc) >= 1.999)), 4)
    if a.phase is not None:
        qa["abd_polarity_flipped"] = a.phase.abd_flipped
    return qa
