#!/usr/bin/env python3
"""One-figure report + per-label statistics for a recorded session.

    python analysis/report.py data/s01.parquet
    python analysis/report.py data/s01.parquet --start 120 --end 300      # zoom (seconds)
    python analysis/report.py data/s01.parquet --config my_thresholds.yaml

Writes reports/<session>.png, <session>_summary.csv, <session>_breaths.csv and
<session>_neck_events.csv, and prints the per-label table and data-quality
numbers. All thresholds come from analysis/config.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "host", REPO / "analysis"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pandas as pd  # noqa: E402

from kman_analysis import analyze, load_config  # noqa: E402
from kman_host.session_io import load_session  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", help="session .parquet")
    ap.add_argument("--config", help="YAML merged over analysis/config.yaml")
    ap.add_argument("--out-dir", default=str(REPO / "reports"))
    ap.add_argument("--start", type=float, help="zoom start (s)")
    ap.add_argument("--end", type=float, help="zoom end (s)")
    ap.add_argument("--show", action="store_true", help="also open an interactive window")
    args = ap.parse_args(argv)
    if (args.start is None) != (args.end is None):
        ap.error("--start and --end must be given together")

    cfg = load_config(args.config)
    s = load_session(args.session)
    a = analyze(s, cfg)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = s.name + (f"_{args.start:g}-{args.end:g}s" if args.start is not None and args.end is not None else "")

    from kman_analysis.plotting import make_figure

    rng = (args.start, args.end) if args.start is not None and args.end is not None else None
    fig = make_figure(a, rng)
    png = out / f"{stem}.png"
    fig.savefig(png, dpi=cfg["report"]["dpi"], facecolor=fig.get_facecolor())

    a.summary.to_csv(out / f"{s.name}_summary.csv", index=False, float_format="%.4g")
    if a.chest is not None:
        pd.DataFrame({"t_s": a.chest.breath_t, "amplitude_deg": a.chest.breath_amp_deg,
                      "valid": a.chest.breath_valid}).to_csv(out / f"{s.name}_breaths.csv", index=False,
                                                             float_format="%.4g")
    if a.neck is not None:
        a.neck.events.to_csv(out / f"{s.name}_neck_events.csv", index=False, float_format="%.4g")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 40)
    print(f"Session {s.name}: {s.header.raw.get('board')} fw {s.header.raw.get('fw_version')}, "
          f"channels {', '.join(s.header.channel_names)}")
    print("Data quality:", json.dumps({k: (v.item() if hasattr(v, "item") else v) for k, v in a.qa.items()}))
    print("\nPer-label summary:")
    print(a.summary.round(2).to_string(index=False))
    if a.qa["lost_pct"] >= 0.1:
        print("\nWARNING: frame loss >= 0.1 % (acceptance limit)")
    print(f"\nFigure: {png}")
    if args.show:
        import matplotlib

        matplotlib.use("TkAgg", force=True)
        import matplotlib.pyplot as plt

        make_figure(a, rng)
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
