"""Chest-abdomen phase via the Hilbert transform.

In-phase (~0 deg) is normal breathing; ~180 deg is paradoxical effort, the
signature of an obstructed airway with continued respiratory effort.

Each waveform's PCA sign is arbitrary, so the abdomen polarity is calibrated
once per session: during the quiet reference (rest breathing) the two are
assumed in phase, and the abdomen is flipped if their correlation is negative.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import signal


@dataclass
class Phase:
    t: np.ndarray
    phase_deg: np.ndarray  # windowed circular mean of chest - abdomen, in (-180, 180]
    coherence: np.ndarray  # resultant length 0..1
    valid: np.ndarray  # amplitude + coherence gate passed
    paradox: np.ndarray  # valid & |phase| > paradox_deg
    abd_flipped: bool


def chest_abdomen_phase(t: np.ndarray, chest: np.ndarray, abd: np.ndarray, fs: float, calib: np.ndarray,
                        invalid: np.ndarray, cfg: dict) -> Phase:
    c = cfg["phase"]
    flipped = False
    if calib.sum() > 10 and np.corrcoef(chest[calib], abd[calib])[0, 1] < 0:
        abd = -abd
        flipped = True
    zc, za = signal.hilbert(chest), signal.hilbert(abd)
    d = np.exp(1j * (np.angle(zc) - np.angle(za)))
    ok = ~invalid
    win = max(3, int(c["window_s"] * fs))
    cnt = pd.Series(ok.astype(float)).rolling(win, center=True, min_periods=1).sum().to_numpy()
    re = pd.Series(np.where(ok, d.real, 0.0)).rolling(win, center=True, min_periods=1).sum().to_numpy()
    im = pd.Series(np.where(ok, d.imag, 0.0)).rolling(win, center=True, min_periods=1).sum().to_numpy()
    z = (re + 1j * im) / np.maximum(cnt, 1)
    phase = np.degrees(np.angle(z))
    coh = np.abs(z)

    ec, ea = np.abs(zc), np.abs(za)
    mc = np.median(ec[calib]) if calib.sum() > 10 else np.median(ec)
    ma = np.median(ea[calib]) if calib.sum() > 10 else np.median(ea)
    amp_ok = (ec > c["min_amplitude_frac"] * mc) & (ea > c["min_amplitude_frac"] * ma)
    amp_ok = pd.Series(amp_ok.astype(float)).rolling(win, center=True, min_periods=1).mean().to_numpy() > 0.5
    valid = amp_ok & (coh >= c["min_coherence"]) & (cnt >= 0.5 * win)
    paradox = valid & (np.abs(phase) > c["paradox_deg"])
    return Phase(t=t, phase_deg=phase, coherence=coh, valid=valid, paradox=paradox, abd_flipped=flipped)
