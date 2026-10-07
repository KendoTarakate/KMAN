"""Session -> uniform time grid in physical units.

Lost frames leave holes in the tick sequence. Everything downstream (filtfilt,
Hilbert, PCA) assumes uniform sampling, so samples are placed on the tick grid
and holes are filled by linear interpolation. Holes longer than
``max_interp_gap_s`` are additionally masked (``gap``) so no breath or event
is reported from interpolated data. Frames whose sensor slot was flagged
invalid are treated as holes for that slot's channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from kman_host.session_io import Session


@dataclass
class Uniform:
    fs: float
    t: np.ndarray  # seconds from the first sample
    label: np.ndarray  # int, forward-filled
    gap: np.ndarray  # bool: inside a long hole
    ch: dict[str, np.ndarray]  # physical units (g, dps, counts), holes filled
    label_names: dict[int, str]
    slots: dict[str, bool] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.t)

    def vec(self, prefix: str, kind: str = "a") -> np.ndarray | None:
        names = [f"{prefix}_{kind}{a}" for a in "xyz"]
        if all(n in self.ch for n in names):
            return np.stack([self.ch[n] for n in names], axis=1)
        return None


def _fill(x: np.ndarray) -> np.ndarray:
    """Linear interpolation over NaNs, nearest value at the edges."""
    bad = np.isnan(x)
    if not bad.any():
        return x
    if bad.all():
        return np.zeros_like(x)
    idx = np.arange(len(x))
    x = x.copy()
    x[bad] = np.interp(idx[bad], idx[~bad], x[~bad])
    return x


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index pairs of True runs."""
    if not mask.any():
        return []
    d = np.diff(np.r_[0, mask.astype(np.int8), 0])
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def to_uniform(session: Session, cfg: dict) -> Uniform:
    df = session.df
    fs = session.fs
    idx = (df["seq"].to_numpy() - int(df["seq"].iloc[0])).astype(np.int64)
    n = int(idx[-1]) + 1
    present = np.zeros(n, dtype=bool)
    present[idx] = True

    label = np.full(n, -1, dtype=np.int64)
    label[idx] = df["label"].to_numpy()
    # forward fill labels over holes
    pos = np.where(label >= 0, np.arange(n), 0)
    np.maximum.accumulate(pos, out=pos)
    label = label[pos]

    max_gap = int(round(cfg["preprocess"]["max_interp_gap_s"] * fs))
    gap = np.zeros(n, dtype=bool)
    for a, b in _runs(~present):
        if b - a > max_gap:
            gap[a:b] = True

    valid = df["valid"].to_numpy()
    ch: dict[str, np.ndarray] = {}
    for c in session.header.channels:
        x = np.full(n, np.nan)
        v = df[c.name].to_numpy(dtype=np.float64) * c.scale
        ok = (valid & (1 << c.bit)) != 0
        x[idx[ok]] = v[ok]
        missing = np.isnan(x)
        for a, b in _runs(missing):
            if b - a > max_gap:
                gap[a:b] = True
        ch[c.name] = _fill(x)

    slots = {s: session.has_slot(s) for s in ("chest_imu", "abdomen_imu", "neck_flex", "neck_imu")}
    return Uniform(fs=fs, t=np.arange(n) / fs, label=label, gap=gap, ch=ch,
                   label_names=session.label_names, slots=slots)


def calibration_mask(u: Uniform, cfg: dict) -> np.ndarray:
    """Samples used as the per-session quiet reference."""
    c = cfg["calibration"]
    m = (u.label == c["label"]) & ~u.gap
    if m.sum() < 5 * u.fs:
        m = (u.t < c["fallback_first_s"]) & ~u.gap
    limit = int(c["max_s"] * u.fs)
    if m.sum() > limit:
        cut = np.flatnonzero(m)[limit]
        m[cut:] = False
    return m


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    return _runs(mask)
