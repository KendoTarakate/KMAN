import numpy as np
import pytest

from kman_host import protocol as P
from kman_host import simulator as sim
from kman_host.session_io import SessionWriter, load_session
from kman_host.stream import LayoutChanged, StreamDecoder

SHORT = [(1, 6), (8, 4), (4, 6)]


def _decode(s, **stream_kw):
    dec = StreamDecoder()
    dec.schedule_labels(s.label_events())
    for _, fr in s.stream(**stream_kw):
        dec.feed(fr, 0.0)
    return dec, dec.drain()


def test_wrap_and_gaps_are_exact():
    cfg = sim.SimConfig(t0_us=2**32 - 2_000_000, seq0=65530)
    s = sim.generate(cfg, SHORT)
    dec, b = _decode(s, drop_prob=0.01, seed=5)
    assert np.all(np.diff(b["t_us"]) > 0)
    assert np.all(np.diff(b["t_us"]) == np.diff(b["seq"]) * 10000)
    idx = b["seq"] - cfg.seq0
    assert dec.stats.dropped == s.n - len(idx)
    assert dec.stats.seq_mismatch == 0
    assert np.array_equal(b["chest_gx"], s.raw["chest_gx"][idx])
    assert np.array_equal(b["label"], s.labels[idx])


def test_corruption_costs_only_the_corrupted_frame():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec, b = _decode(s, corrupt_prob=0.01, seed=2)
    assert dec.parser.stats.crc_errors > 0
    assert dec.stats.dropped == dec.parser.stats.crc_errors  # each corrupted frame is one gap tick
    assert len(b["t_us"]) + dec.stats.dropped == s.n


def test_live_label_applies_from_next_frame():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    stream = list(s.stream())
    for _, fr in stream[:51]:  # header + 50 data frames
        dec.feed(fr, 0.0)
    ev = dec.set_label(4)
    assert ev.seq == dec.last_seq64 + 1
    for _, fr in stream[51:60]:
        dec.feed(fr, 0.0)
    b = dec.drain()
    assert (b["label"][:50] == 0).all() and (b["label"][50:] == 4).all()


def test_frames_before_header_are_counted():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    for i in range(5):
        dec.feed(s.data_frame(i), 0.0)
    assert dec.stats.frames_before_header == 5 and dec.drain() is None


def test_layout_change_raises():
    s = sim.generate(sim.SimConfig(), SHORT)
    s2 = sim.generate(sim.SimConfig(abdomen=True), SHORT)
    dec = StreamDecoder()
    dec.feed(s.header_frame(), 0.0)
    with pytest.raises(LayoutChanged):
        dec.feed(s2.header_frame(), 0.0)


def test_device_restart_starts_new_segment():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    dec.feed(s.header_frame(), 100.0)
    for i in range(10):
        dec.feed(s.data_frame(i), 100.0 + i * 0.01)
    hdr2 = dict(s.header, stream_id=2)
    dec.feed(P.encode_frame(P.TYPE_HEADER, __import__("json").dumps(hdr2).encode()), 102.0)
    for i in range(5):  # device clock restarted: same t32 values again
        dec.feed(s.data_frame(i), 102.0 + i * 0.01)
    b = dec.drain()
    assert list(np.unique(b["segment"])) == [0, 1]
    assert np.all(np.diff(b["t_us"]) > 0)
    gap = b["t_us"][10] - b["t_us"][9]
    assert 1.8e6 < gap < 2.2e6  # bridged with the ~1.9 s host-measured gap


def test_session_roundtrip(tmp_path):
    s = sim.generate(sim.SimConfig(abdomen=True), SHORT)
    dec = StreamDecoder()
    dec.schedule_labels(s.label_events())
    w = SessionWriter(tmp_path / "x.parquet", row_group_rows=200)
    for _, fr in s.stream():
        w.write_raw(fr)
        dec.feed(fr, 0.0)
        if dec.new_header:
            w.set_header(dec.header)
            dec.new_header = False
        if dec.pending_rows() >= 77:
            w.write_batch(dec.drain())
    w.write_batch(dec.drain())
    w.close(dec.headers, dec.label_events_dicts(), dec.summary())
    ss = load_session(tmp_path / "x.parquet")
    assert len(ss.df) == s.n
    assert ss.has_slot("abdomen_imu")
    assert [e["label"] for e in ss.labels] == [1, 8, 4]
    ph = ss.physical()
    assert ph["chest_ay"].abs().mean() == pytest.approx(0.97, abs=0.02)  # ~1 g gravity component
    assert (tmp_path / "x.bin").stat().st_size > 0
