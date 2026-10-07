#!/usr/bin/env python3
"""Export a session to CSV for quick inspection.

    python host/to_csv.py data/s01.parquet                 # -> data/s01.csv (raw counts)
    python host/to_csv.py data/s01.parquet --units         # physical units (g, dps), NaN = invalid
    python host/to_csv.py data/s01.parquet --labels-only   # label events -> data/s01_labels.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from kman_host.session_io import load_session


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", help="session .parquet")
    ap.add_argument("--out", help="output .csv (default: next to the session)")
    ap.add_argument("--units", action="store_true", help="convert channels to physical units")
    ap.add_argument("--labels-only", action="store_true", help="export the label events instead of samples")
    args = ap.parse_args(argv)

    s = load_session(args.session)
    src = Path(args.session)
    if args.labels_only:
        out = Path(args.out) if args.out else src.with_name(src.stem + "_labels.csv")
        pd.DataFrame(s.labels).to_csv(out, index=False)
        print(f"wrote {len(s.labels)} label events to {out}")
        return 0

    out = Path(args.out) if args.out else src.with_suffix(".csv")
    if args.units:
        df = s.physical()
    else:
        df = s.df.copy()
        df.insert(0, "t_s", (df["t_us"] - df["t_us"].iloc[0]) / 1e6)
    df.insert(df.columns.get_loc("label") + 1, "label_name", df["label"].map(s.label_names))
    df.to_csv(out, index=False, float_format="%.6g")
    print(f"wrote {len(df)} rows x {len(df.columns)} columns to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
