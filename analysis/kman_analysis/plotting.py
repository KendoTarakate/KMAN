"""Session report figure: all signals stacked on one time axis, labels as
background bands.

Colour use: the label bands own the categorical hues (fixed order, see
kman_host.labels). Data marks are therefore drawn in neutral ink, and event
types are distinguished by marker shape and fill plus a legend, so no
identity depends on colour alone. Each band is also numbered with its label
code at the top of the figure.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from kman_host.labels import HATCHED_LABELS, LABEL_COLORS_LIGHT  # noqa: E402

from .pipeline import Analysis  # noqa: E402
from .preprocess import runs  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
INK_3 = "#8a8984"
GRID = "#e4e3de"
# Legends sit in the panel's title row, right-aligned, clear of the data.
LEGEND = dict(loc="lower right", bbox_to_anchor=(1.0, 1.0), fontsize=7, frameon=False, borderaxespad=0.2)


def _style(ax, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK_3)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=8)


def _title(ax, text: str, pad: float = 3) -> None:
    ax.set_title(text, loc="left", color=INK, fontsize=9.5, fontweight="bold", pad=pad)


def _bands(ax, a: Analysis, ts: float, alpha: float, numbers: bool, t_range=None) -> None:
    u = a.u
    lo, hi = (t_range[0] / ts, t_range[1] / ts) if t_range else (-np.inf, np.inf)
    for lab in np.unique(u.label):
        if lab == 0 or lab not in LABEL_COLORS_LIGHT:
            continue
        col = LABEL_COLORS_LIGHT[int(lab)]
        for s, e in runs(u.label == lab):
            x0, x1 = s / u.fs / ts, e / u.fs / ts
            ax.axvspan(x0, x1, color=col, alpha=alpha, linewidth=0, zorder=0)
            if lab in HATCHED_LABELS:
                ax.axvspan(x0, x1, facecolor="none", edgecolor=INK_3, hatch="///", linewidth=0, zorder=0)
            xm = (max(x0, lo) + min(x1, hi)) / 2
            if numbers and lo <= xm <= hi:
                ax.text(xm, 0.97, str(int(lab)), transform=ax.get_xaxis_transform(), ha="center",
                        va="top", fontsize=7.5, fontweight="bold", color=INK_2, clip_on=True, zorder=5)


def _artifact_spans(ax, a: Analysis, ts: float) -> None:
    q_mask = a.motion | a.u.gap
    for s, e in runs(q_mask):
        ax.axvspan(s / a.u.fs / ts, e / a.u.fs / ts, facecolor="none", edgecolor=INK_3, hatch="xx",
                   linewidth=0, zorder=0.5)


def make_figure(a: Analysis, t_range: tuple[float, float] | None = None):
    cfg = a.cfg
    rc = cfg["report"]
    u = a.u
    dur = u.n / u.fs
    ts = 60.0 if dur > 600 else 1.0
    tunit = "min" if ts == 60.0 else "s"

    panels = []
    if a.chest is not None:
        panels += ["resp_chest"]
    if a.abd is not None:
        panels += ["resp_abd"]
    if a.chest is not None:
        panels += ["rate", "since"]
    if a.phase is not None:
        panels += ["phase"]
    if a.neck is not None:
        panels += ["flex", "neck_rate"]
    if a.motion_mg is not None:
        panels += ["motion"]
    heights = [1.25 if p.startswith("resp") or p == "flex" else 0.85 for p in panels]

    fig = plt.figure(figsize=(rc["width_in"], rc["panel_height_in"] * sum(heights) + 3.2), facecolor=SURFACE)
    gs = fig.add_gridspec(len(panels) + 1, 1, height_ratios=heights + [1.6], hspace=0.42,
                          left=0.05, right=0.985, top=0.95, bottom=0.03)
    axes = []
    for i, p in enumerate(panels):
        ax = fig.add_subplot(gs[i], sharex=axes[0] if axes else None)
        axes.append(ax)
        _bands(ax, a, ts, rc["band_alpha"], numbers=(i == 0), t_range=t_range)
        if p in ("resp_chest", "resp_abd", "rate", "since"):
            _artifact_spans(ax, a, ts)

        if p in ("resp_chest", "resp_abd"):
            br = a.chest if p == "resp_chest" else a.abd
            _style(ax, "tilt (deg)")
            _title(ax, f"Respiration, {'chest' if p == 'resp_chest' else 'abdomen'} "
                       "(complementary-filter tilt, 0.05-1 Hz, PC1; peaks = end-inspiration)")
            ax.plot(br.t / ts, br.wave_deg, color=INK, linewidth=0.8, zorder=2)
            bi = np.clip(np.round(br.breath_t * br.fs).astype(int), 0, len(br.wave_deg) - 1)
            v = br.breath_valid
            ax.plot(br.breath_t[v] / ts, br.wave_deg[bi[v]], "o", ms=3.5, color=INK, zorder=3, label="breath")
            ax.plot(br.breath_t[~v] / ts, br.wave_deg[bi[~v]], "o", ms=3.5, mfc="none", mec=INK_2, zorder=3,
                    label="breath during artifact/gap (not counted)")
            lim = np.nanpercentile(np.abs(br.wave_deg), 99.5) * 1.3 or 1.0
            ax.set_ylim(-lim, lim)
            if p == "resp_chest":
                ax.legend(handles=[*ax.get_legend_handles_labels()[0],
                                   Patch(facecolor="none", edgecolor=INK_3, hatch="xx", label="motion artifact / gap")],
                          **LEGEND, ncol=3)
        elif p == "rate":
            _style(ax, "breaths/min")
            _title(ax, "Breath rate (per valid breath interval)")
            ax.plot(a.chest.rate_t / ts, a.chest.rate_bpm, ".", ms=4, color=INK)
            ax.set_ylim(0, max(30, np.nanpercentile(a.chest.rate_bpm, 99) * 1.15) if len(a.chest.rate_bpm) else 30)
        elif p == "since":
            _style(ax, "s")
            _title(ax, "Seconds since last valid breath")
            ax.plot(u.t / ts, a.chest.since_last_s, color=INK, linewidth=1.0)
            ax.set_ylim(bottom=0)
        elif p == "phase":
            ph = a.phase
            _style(ax, "deg")
            _title(ax, "Chest - abdomen phase (Hilbert, windowed circular mean; 0 = in phase, +-180 = paradoxical)")
            y = np.where(ph.valid, ph.phase_deg, np.nan)
            ax.plot(ph.t / ts, y, color=INK, linewidth=1.2)
            thr = cfg["phase"]["paradox_deg"]
            for s in (thr, -thr):
                ax.axhline(s, color=INK_2, linewidth=0.8, linestyle="--")
            ax.text(1.0, thr, f" paradox > {thr:g}", transform=ax.get_yaxis_transform(), va="bottom", ha="right",
                    fontsize=7, color=INK_2)
            ax.set_ylim(-185, 185)
            ax.set_yticks([-180, -90, 0, 90, 180])
        elif p == "flex":
            ne = a.neck
            c = cfg["swallow"]
            _style(ax, "counts")
            _title(ax, f"Neck flex deviation from {c['baseline_window_s']:g} s moving-median baseline "
                       f"(sigma_cal = {ne.sigma_cal:.1f} counts)")
            ax.plot(u.t / ts, ne.deviation, color=INK, linewidth=0.7, zorder=2)
            for k, ls in ((c["k_on"], "--"), (c["head_amplitude_k"], ":")):
                for sgn in (1, -1):
                    if c["polarity"] == "positive" and sgn < 0 or c["polarity"] == "negative" and sgn > 0:
                        continue
                    ax.axhline(sgn * k * ne.sigma_cal, color=INK_2, linewidth=0.8, linestyle=ls, zorder=1)
            ev = ne.events
            sw = ev[ev["kind"] == "swallow"]
            hd = ev[ev["kind"] == "head_motion"]
            ax.plot(sw["peak_s"] / ts, sw["peak_dev"], "^", ms=6, color=INK, zorder=4, label=f"swallow ({len(sw)})")
            ax.plot(hd["peak_s"] / ts, hd["peak_dev"], "s", ms=5.5, mfc="none", mec=INK, mew=1.2, zorder=4,
                    label=f"head motion ({len(hd)})")
            ax.plot([], [], "--", color=INK_2, linewidth=0.8, label=f"event threshold {c['k_on']:g} sigma")
            ax.plot([], [], ":", color=INK_2, linewidth=0.8, label=f"head-motion amplitude {c['head_amplitude_k']:g} sigma")
            ax.legend(**LEGEND, ncol=4)
            lim = np.nanpercentile(np.abs(ne.deviation), 99.8) * 1.25 or 1.0
            ax.set_ylim(-lim, lim)
        elif p == "neck_rate":
            ne = a.neck
            w = cfg["swallow"]["rate_window_s"]
            _style(ax, f"events / {w:g} s")
            _title(ax, f"Neck events per {w:g} s (centred window)")
            ax.plot(u.t / ts, ne.swallow_rate_10s, color=INK, linewidth=1.2, label="swallows")
            ax.plot(u.t / ts, ne.head_rate_10s, color=INK_2, linewidth=1.2, linestyle="--", label="head motion")
            ax.legend(**LEGEND, ncol=2)
            ax.set_ylim(bottom=0)
        elif p == "motion":
            _style(ax, "mg rms")
            _title(ax, f"Chest motion energy > {cfg['motion']['highpass_hz']:g} Hz "
                       f"({cfg['motion']['window_s']:g} s RMS; artifact above dashed line)")
            ax.semilogy(u.t / ts, np.maximum(a.motion_mg, 1e-2), color=INK, linewidth=0.8)
            ax.axhline(cfg["motion"]["threshold_mg"], color=INK_2, linewidth=0.8, linestyle="--")

    for ax in axes[:-1]:
        plt.setp(ax.get_xticklabels(), visible=False)
    axes[-1].set_xlabel(f"time ({tunit})", color=INK_2, fontsize=8)
    if t_range:
        axes[0].set_xlim(t_range[0] / ts, t_range[1] / ts)
    else:
        axes[0].set_xlim(0, dur / ts)

    # label legend (direct labels = numbers on the top band row)
    present = [int(x) for x in np.unique(u.label) if x in LABEL_COLORS_LIGHT]
    handles = [Patch(facecolor=LABEL_COLORS_LIGHT[k], alpha=0.5, edgecolor=INK_3 if k in HATCHED_LABELS else "none",
                     hatch="///" if k in HATCHED_LABELS else None, label=f"{k} {u.label_names.get(k, k)}")
               for k in present]
    qa = a.qa
    fig.suptitle(f"{a.session.name}   |   {qa['duration_s']:.0f} s, lost {qa['lost_pct']:.3f} %, "
                 f"CRC {qa['crc_errors']}, segments {qa['segments']}, artifact {qa['artifact_pct']:.1f} %",
                 x=0.05, ha="left", color=INK, fontsize=11, fontweight="bold")
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.985, 0.985), ncol=min(9, len(handles)),
               fontsize=7.5, frameon=False)

    # per-label summary table
    tax = fig.add_subplot(gs[-1])
    tax.axis("off")
    cols = [("label", "label", "{:d}"), ("name", "activity", "{}"), ("duration_s", "dur s", "{:.0f}"),
            ("breaths_per_min", "breaths/min", "{:.1f}"), ("amp_median_deg", "amp deg", "{:.2f}"),
            ("max_breath_gap_s", "max gap s", "{:.1f}"), ("paradox_pct", "paradox %", "{:.0f}"),
            ("swallows_per_10s", "swallows/10s", "{:.2f}"), ("head_events_per_10s", "head/10s", "{:.2f}"),
            ("artifact_pct", "artifact %", "{:.1f}")]
    cols = [c for c in cols if c[0] in a.summary.columns]

    def fmt(v, f):
        try:
            return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f.format(v)
        except (ValueError, TypeError):
            return str(v)

    cells = [[fmt(r[c], f) for c, _, f in cols] for _, r in a.summary.iterrows()]
    if cells:
        tbl = tax.table(cellText=cells, colLabels=[h for _, h, _ in cols], loc="upper left", cellLoc="right",
                        colLoc="right", edges="horizontal")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8)
        tbl.scale(1, 1.15)
        for (r, c), cell in tbl.get_celld().items():
            cell.set_edgecolor(GRID)
            cell.get_text().set_color(INK if r else INK_2)
            if c == 1:
                cell.get_text().set_ha("left")
                cell._loc = "left"
    return fig
