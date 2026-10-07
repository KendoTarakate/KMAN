"""Session files.

A recording produces up to three files sharing a stem:

``<stem>.parquet``       samples (one row per frame) + JSON metadata in the
                         Parquet key/value store (session header(s), label
                         events, label names, logger/device statistics).
``<stem>.bin``           every byte received from the device, verbatim. Written
                         live, so a crash loses nothing; ``logger.py --replay``
                         rebuilds the Parquet file from it.
``<stem>.labels.jsonl``  label events, appended and flushed on every key press.

Sample columns: t_us (int64, unwrapped device time), seq (int64, unwrapped tick
index), segment (int16), valid (uint8 per-slot bitmask), one int16 column per
raw channel, label (int8), host_time (float64 unix seconds at receipt).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .labels import LABELS
from .protocol import SessionHeader

META_PREFIX = "kman."
FORMAT_VERSION = 1


def sidecar_paths(parquet_path: str | Path) -> tuple[Path, Path]:
    p = Path(parquet_path)
    stem = p.with_suffix("")
    return stem.with_suffix(".bin"), Path(str(stem) + ".labels.jsonl")


class SessionWriter:
    def __init__(
        self,
        path: str | Path,
        raw: bool = True,
        row_group_rows: int = 3000,
        extra_meta: dict | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw_path, labels_path = sidecar_paths(self.path)
        self.raw_path = raw_path if raw else None
        self.labels_path = labels_path
        self._raw = open(raw_path, "wb") if raw else None
        self._labels = open(labels_path, "w", encoding="utf-8")
        self._writer: pq.ParquetWriter | None = None
        self._schema: pa.Schema | None = None
        self._pending: list[dict[str, np.ndarray]] = []
        self._pending_rows = 0
        self._row_group_rows = row_group_rows
        self.rows_written = 0
        self.header: SessionHeader | None = None
        self.extra_meta = dict(extra_meta or {})
        self.extra_meta.setdefault("created_unix", time.time())
        self._last_raw_flush = time.monotonic()

    # -------------------------------------------------------------- live sidecars

    def write_raw(self, data: bytes) -> None:
        if self._raw and data:
            self._raw.write(data)
            now = time.monotonic()
            if now - self._last_raw_flush > 1.0:
                self._raw.flush()
                self._last_raw_flush = now

    def write_label_event(self, event: dict) -> None:
        self._labels.write(json.dumps(event) + "\n")
        self._labels.flush()

    # -------------------------------------------------------------- samples

    def set_header(self, header: SessionHeader) -> None:
        if self.header is None:
            self.header = header

    def _open(self) -> None:
        assert self.header is not None
        fields = [
            pa.field("t_us", pa.int64()),
            pa.field("seq", pa.int64()),
            pa.field("segment", pa.int16()),
            pa.field("valid", pa.uint8()),
        ]
        fields += [pa.field(n, pa.int16()) for n in self.header.channel_names]
        fields += [pa.field("label", pa.int8()), pa.field("host_time", pa.float64())]
        meta = {
            META_PREFIX + "format_version": str(FORMAT_VERSION),
            META_PREFIX + "header": json.dumps(self.header.raw),
        }
        self._schema = pa.schema(fields, metadata=meta)
        self._writer = pq.ParquetWriter(self.path, self._schema, compression="zstd")

    def write_batch(self, batch: dict[str, np.ndarray]) -> None:
        if self.header is None:
            raise RuntimeError("write_batch before set_header")
        self._pending.append(batch)
        self._pending_rows += len(batch["t_us"])
        if self._pending_rows >= self._row_group_rows:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        if self._writer is None:
            self._open()
        cols = {name: np.concatenate([b[name] for b in self._pending]) for name in self._schema.names}
        table = pa.Table.from_pydict(cols, schema=self._schema)
        self._writer.write_table(table)
        self.rows_written += table.num_rows
        self._pending.clear()
        self._pending_rows = 0
        if self._raw:
            self._raw.flush()

    def close(self, headers: list[dict], label_events: list[dict], stats: dict, notes: str = "") -> None:
        self.flush()
        if self._writer is not None:
            meta = {
                META_PREFIX + "headers": json.dumps(headers),
                META_PREFIX + "labels": json.dumps(label_events),
                META_PREFIX + "label_names": json.dumps({str(k): v for k, v in LABELS.items()}),
                META_PREFIX + "stats": json.dumps(stats),
                META_PREFIX + "extra": json.dumps(self.extra_meta),
                META_PREFIX + "notes": notes,
            }
            self._writer.add_key_value_metadata(meta)
            self._writer.close()
            self._writer = None
        if self._raw:
            self._raw.close()
            self._raw = None
        self._labels.close()


# ------------------------------------------------------------------------- reading


@dataclass
class Session:
    path: Path
    df: pd.DataFrame
    header: SessionHeader
    labels: list[dict]
    label_names: dict[int, str]
    stats: dict = field(default_factory=dict)
    headers: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def fs(self) -> float:
        return self.header.sample_rate_hz

    @property
    def name(self) -> str:
        return self.path.stem

    def has_slot(self, slot: str) -> bool:
        return self.header.slot_present(slot)

    def physical(self) -> pd.DataFrame:
        """Channels converted to physical units (float64); samples whose slot
        was flagged invalid in that frame become NaN."""
        out = pd.DataFrame({"t_s": (self.df["t_us"] - self.df["t_us"].iloc[0]) / 1e6})
        valid = self.df["valid"].to_numpy()
        for ch in self.header.channels:
            v = self.df[ch.name].to_numpy(dtype=np.float64) * ch.scale
            v[(valid & (1 << ch.bit)) == 0] = np.nan
            out[ch.name] = v
        out["label"] = self.df["label"].to_numpy()
        out["seq"] = self.df["seq"].to_numpy()
        out["segment"] = self.df["segment"].to_numpy()
        return out


def load_session(path: str | Path) -> Session:
    path = Path(path)
    pf = pq.ParquetFile(path)
    kv = {k.decode(): v.decode() for k, v in (pf.metadata.metadata or {}).items()}
    header = SessionHeader.from_json(kv[META_PREFIX + "header"])
    df = pf.read().to_pandas()
    names = json.loads(kv.get(META_PREFIX + "label_names", "{}")) or {str(k): v for k, v in LABELS.items()}
    return Session(
        path=path,
        df=df,
        header=header,
        labels=json.loads(kv.get(META_PREFIX + "labels", "[]")),
        label_names={int(k): v for k, v in names.items()},
        stats=json.loads(kv.get(META_PREFIX + "stats", "{}")),
        headers=json.loads(kv.get(META_PREFIX + "headers", "[]")),
        extra=json.loads(kv.get(META_PREFIX + "extra", "{}")),
        notes=kv.get(META_PREFIX + "notes", ""),
    )


def read_label_events(path: str | Path) -> list[dict]:
    events = []
    p = Path(path)
    if not p.exists():
        return events
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # torn last line after a crash
    return events
