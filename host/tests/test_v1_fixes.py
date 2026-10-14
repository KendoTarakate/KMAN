"""Regression tests for the issues found by the code review before 1.0
(see CHANGELOG.md, "Fixed")."""

import json

import numpy as np
import pytest

import logger
from kman_host import protocol as P
from kman_host import simulator as sim
from kman_host.session_io import load_session, read_label_events, sidecar_paths
from kman_host.stream import StreamDecoder
from live_view import Ring

SHORT = [(1, 6), (8, 4), (4, 6)]


def _feed(dec, s, frames, t0_host):
    for k, i in enumerate(frames):
        dec.feed(s.data_frame(i), t0_host + k * 0.01)


# ---------------------------------------------------------------- C: device restarts


def test_reboot_with_same_stream_id_keeps_data():
    """Old firmware (no boot_id) reboots: stream_id is 1 again and the clock restarts.
    The header's timestamp gives the restart away; nothing may be thrown away."""
    s1 = sim.generate(sim.SimConfig(t0_us=60_000_000), SHORT)
    s2 = sim.generate(sim.SimConfig(t0_us=1_500_000, seq0=150), SHORT)
    for s in (s1, s2):
        s.header.pop("boot_id")
    dec = StreamDecoder()
    dec.feed(s1.header_frame(), 100.0)
    _feed(dec, s1, range(500), 100.0)
    dec.feed(s2.header_frame(), 110.0)
    _feed(dec, s2, range(500), 110.0)
    b = dec.drain()
    assert len(b["t_us"]) == 1000 and dec.stats.duplicates == 0
    assert dec.stats.segments == 2
    assert np.all(np.diff(b["t_us"]) > 0)


def test_reboot_after_long_uptime_is_not_a_forward_gap():
    """Device up 50 min (clock near 3e9 us) reboots: modular arithmetic alone would read the
    restart as a 21-minute forward jump and count ~130k lost frames."""
    s1 = sim.generate(sim.SimConfig(t0_us=3_000_000_000), SHORT)
    s2 = sim.generate(sim.SimConfig(t0_us=1_200_000, seq0=120), SHORT)
    dec = StreamDecoder()
    dec.feed(s1.header_frame(), 100.0)
    _feed(dec, s1, range(300), 100.0)
    dec.feed(s2.header_frame(), 105.0)
    _feed(dec, s2, range(300), 105.0)
    assert dec.stats.dropped == 0 and dec.stats.segments == 2
    assert dec.pending_rows() == 600


def test_boot_id_change_starts_segment():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    dec.feed(s.header_frame(), 0.0)
    _feed(dec, s, range(50), 0.0)
    hdr = dict(s.header, boot_id="0xDEADBEEF", t_us_at_header=s.cfg.t0_us + 50 * 10000)
    dec.feed(P.encode_frame(P.TYPE_HEADER, json.dumps(hdr).encode()), 1.0)
    assert dec.stats.segments == 2


def test_resent_header_mid_stream_is_not_a_new_segment():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    dec.feed(s.header_frame(), 0.0)
    _feed(dec, s, range(400), 0.0)
    dec.feed(s.header_frame(400), 4.0)  # answer to 'H'
    _feed(dec, s, range(400, 600), 4.0)
    assert dec.stats.segments == 1 and dec.stats.dropped == 0
    assert dec.pending_rows() == 600


def test_restart_without_header_is_detected_from_the_data():
    s1 = sim.generate(sim.SimConfig(t0_us=30_000_000), SHORT)
    s2 = sim.generate(sim.SimConfig(t0_us=900_000, seq0=90), SHORT)
    dec = StreamDecoder()
    dec.feed(s1.header_frame(), 0.0)
    _feed(dec, s1, range(200), 0.0)
    _feed(dec, s2, range(200), 5.0)  # header missed
    assert dec.pending_rows() == 400
    assert dec.stats.restarts_detected == 1 and dec.stats.duplicates == 0


def test_true_duplicate_is_still_rejected():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    dec.feed(s.header_frame(), 0.0)
    for i in (0, 1, 2, 2, 3):
        dec.feed(s.data_frame(i), 0.0)
    assert dec.pending_rows() == 4 and dec.stats.duplicates == 1


# ---------------------------------------------------------------- A, H: replay


def _write_sim_session(tmp_path, name="X"):
    import sim_device

    sim_device.main(["--write-session", str(tmp_path / f"{name}.parquet"), "--time-scale", "0.1"])
    return tmp_path / f"{name}.parquet"


def test_replay_into_same_stem_keeps_labels(tmp_path):
    pq = _write_sim_session(tmp_path)
    bin_path, labels_path = sidecar_paths(pq)
    before = read_label_events(labels_path)
    assert len(before) > 3
    pq.unlink()  # crash before the Parquet file was finalised
    assert logger.main(["--replay", str(bin_path), "--out", str(pq)]) == 0
    assert read_label_events(labels_path) == before  # sidecar intact
    assert [e["label"] for e in load_session(pq).labels] == [e["label"] for e in before]
    # a second recovery still has the labels
    assert logger.main(["--replay", str(bin_path), "--out", str(pq), "--force"]) == 0
    assert read_label_events(labels_path) == before


def test_replay_stops_cleanly_on_layout_change(tmp_path):
    s1 = sim.generate(sim.SimConfig(), SHORT)
    s2 = sim.generate(sim.SimConfig(abdomen=True), SHORT)
    data = s1.header_frame() + b"".join(s1.data_frame(i) for i in range(300))
    data += s2.header_frame() + b"".join(s2.data_frame(i) for i in range(100))
    src = tmp_path / "mixed.bin"
    src.write_bytes(data)
    out = tmp_path / "mixed_out.parquet"
    assert logger.main(["--replay", str(src), "--out", str(out)]) == 0
    assert len(load_session(out).df) == 300  # finalised, holds the data before the change


# ---------------------------------------------------------------- B: reconnect


class FakeSerial:
    """Stands in for pyserial. The first port 'unplugs' after a few reads the way
    pyserial 3.5 does on Linux/macOS: in_waiting raises a bare OSError."""

    instances = []
    chunks: list[bytes] = []

    def __init__(self, port, baud, timeout=None):
        self.n = len(FakeSerial.instances)
        self.reads = 0
        FakeSerial.instances.append(self)

    unplugged = False

    @property
    def in_waiting(self):
        if self.n == 0 and self.reads >= 20:
            FakeSerial.unplugged = True
            raise OSError(5, "Input/output error")
        return len(FakeSerial.chunks[0]) if FakeSerial.chunks else 0

    def read(self, n):
        self.reads += 1
        return FakeSerial.chunks.pop(0) if FakeSerial.chunks else b""

    def write(self, data):
        return len(data)

    def close(self):
        pass


def test_logger_reconnects_after_posix_unplug(tmp_path, monkeypatch):
    import serial

    s = sim.generate(sim.SimConfig(), SHORT)
    FakeSerial.instances, FakeSerial.unplugged = [], False
    FakeSerial.chunks = [s.header_frame()] + [b"".join(s.data_frame(i) for i in range(k, k + 10))
                                              for k in range(0, 600, 10)]
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    out = tmp_path / "rec.parquet"
    assert logger.main(["--port", "FAKE", "--out", str(out), "--duration", "2.5", "--no-udp"]) == 0
    assert len(FakeSerial.instances) >= 2  # reopened after the unplug
    sess = load_session(out)
    assert len(sess.df) == 600
    assert "reconnected" in sess.notes


# ---------------------------------------------------------------- D: breath rate


def test_rate_does_not_bridge_an_invalid_breath():
    from kman_analysis import load_config
    from kman_analysis.breathing import detect_breaths

    fs = 20.0
    t = np.arange(0, 60, 1 / fs)
    wave = 0.5 * np.sin(2 * np.pi * (15 / 60) * t)  # 15 breaths/min
    invalid = np.zeros(len(t), dtype=bool)
    invalid[int(29 * fs):int(31 * fs)] = True  # one breath under an artifact
    br = detect_breaths(t, wave, fs, invalid, load_config(), n_full=len(t) * 5, fs_full=100.0)
    assert (~br.breath_valid).sum() == 1
    assert np.allclose(br.rate_bpm, 15.0, atol=0.5)  # no 7.5 bpm interval


# ---------------------------------------------------------------- E: live view ring


@pytest.mark.parametrize("pre", [0, 3, 7])
def test_ring_large_batch_keeps_order(pre):
    r = Ring(5)
    seen = []
    if pre:
        r.extend(np.arange(pre))
        seen += list(range(pre))
    big = np.arange(100, 112)
    r.extend(big)
    seen += list(big)
    r.extend([7, 8])
    seen += [7, 8]
    assert r.ordered()[:, 0].tolist() == seen[-5:]


# ---------------------------------------------------------------- review round 2


def _reboot_stream(gap_host_s=5.0, label_at=300):
    """Live session: 1000 frames, reboot (new boot_id) after a host gap, label pressed
    `label_at` frames into the new segment. Returns (raw bytes, label events, live labels)."""
    from dataclasses import asdict

    s1 = sim.generate(sim.SimConfig(t0_us=40_000_000), SHORT)
    s2 = sim.generate(sim.SimConfig(t0_us=1_100_000, seq0=110), SHORT)
    s2.header["boot_id"] = "0x00000002"
    dec = StreamDecoder()
    raw, t = bytearray(), 100.0
    for fr in [s1.header_frame()] + [s1.data_frame(i) for i in range(1000)]:
        raw += fr
        dec.feed(fr, t)
        t += 0.01
    t += gap_host_s
    events = []
    for i, fr in enumerate([s2.header_frame()] + [s2.data_frame(i) for i in range(1000)]):
        if i == label_at + 1:
            events.append(asdict(dec.set_label(4, t)))
        raw += fr
        dec.feed(fr, t)
        t += 0.01
    return bytes(raw), events, dec.drain()["label"]


def test_replay_places_labels_like_live_after_a_reboot_gap():
    raw, events, live = _reboot_stream()
    rep = StreamDecoder()
    rep.schedule_labels(json.loads(json.dumps(events)))  # as read back from .labels.jsonl
    rep.feed(raw, 0.0)  # replay: no host clock
    replay = rep.drain()["label"]
    assert len(live) == len(replay) == 2000
    assert np.array_equal(live, replay)
    assert int(np.argmax(live == 4)) == 1300


def test_legacy_seq_only_label_events_still_replay():
    s = sim.generate(sim.SimConfig(), SHORT)
    dec = StreamDecoder()
    dec.schedule_labels([{"seq": s.cfg.seq0 + 50, "label": 2}])  # pre-1.0 sidecar line
    for _, fr in s.stream():
        dec.feed(fr, 0.0)
    lab = dec.drain()["label"]
    assert (lab[:50] == 0).all() and (lab[50:] == 2).all()


def test_reboot_after_long_uptime_without_header_is_detected():
    """Uptime 50 min (clock >= 2^31) and the start-up header is missed: the new clock looks
    like a forward step, but the tick counter disagrees, so it is a restart, not 30 min of loss."""
    count = 50 * 60 * 100  # ticks
    s1 = sim.generate(sim.SimConfig(t0_us=(count * 10000) % 2**32, seq0=count % 2**16), SHORT)
    s2 = sim.generate(sim.SimConfig(t0_us=1_200_000, seq0=120), SHORT)
    dec = StreamDecoder()
    dec.feed(s1.header_frame(), 0.0)
    _feed(dec, s1, range(200), 0.0)
    _feed(dec, s2, range(200), 10.0)  # header lost
    assert dec.stats.restarts_detected == 1
    assert dec.stats.dropped == 0 and dec.pending_rows() == 400


def test_reannounced_header_does_not_start_viewer_segments():
    """The logger re-broadcasts the header every 2 s; a viewer must not treat it as a restart."""
    s = sim.generate(sim.SimConfig(), SHORT)
    rec = logger.Recorder.__new__(logger.Recorder)  # only feed()'s header handling is needed
    rec.dec, rec.udp, rec.last_header_raw = StreamDecoder(), None, None

    class _W:
        header = None

        def write_raw(self, d):
            pass

        def set_header(self, h):
            self.header = h

        def write_batch(self, b):
            pass

    rec.writer = _W()
    rec.feed(s.header_frame(), 0.0)
    assert b"t_us_at_header" not in rec.last_header_raw
    viewer = StreamDecoder()
    viewer.feed(s.header_frame(), 0.0)
    for k in range(5):  # 5 s of data, re-announce every second
        _feed(viewer, s, range(k * 100, (k + 1) * 100), float(k))
        viewer.feed(rec.last_header_raw, float(k) + 0.99)
        viewer.feed(s.header_frame(), float(k) + 0.99)  # identical copy of the original too
    assert viewer.stats.segments == 1 and viewer.stats.dropped == 0


class FakeKeys:
    """KeyReader stand-in: types '7' once while the logger waits to reconnect."""

    typed = False

    def __init__(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def poll(self):
        # only after the unplug and before the port reopens: i.e. inside the reconnect wait
        if FakeSerial.unplugged and len(FakeSerial.instances) == 1 and not FakeKeys.typed:
            FakeKeys.typed = True
            return ["7"]
        return []


def test_label_typed_while_reconnecting_is_kept(tmp_path, monkeypatch):
    import serial

    s = sim.generate(sim.SimConfig(), SHORT)
    FakeSerial.instances, FakeKeys.typed, FakeSerial.unplugged = [], False, False
    FakeSerial.chunks = [s.header_frame()] + [b"".join(s.data_frame(i) for i in range(k, k + 10))
                                              for k in range(0, 600, 10)]
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr(logger, "KeyReader", FakeKeys)
    out = tmp_path / "rec.parquet"
    assert logger.main(["--port", "FAKE", "--out", str(out), "--duration", "2.5", "--no-udp"]) == 0
    sess = load_session(out)
    assert [e["label"] for e in sess.labels] == [7]
    lab = sess.df["label"].to_numpy()
    assert (lab[:190] == 0).all() and (lab[-100:] == 7).all()  # applies after the reconnect


class DeadSerial(FakeSerial):
    """The device never comes back: every reopen fails."""

    def __init__(self, port, baud, timeout=None):
        if FakeSerial.instances:
            raise OSError(2, "No such file or directory")
        super().__init__(port, baud, timeout)


def test_duration_is_honoured_while_waiting_to_reconnect(tmp_path, monkeypatch):
    import time

    import serial

    s = sim.generate(sim.SimConfig(), SHORT)
    FakeSerial.instances = []
    FakeSerial.chunks = [s.header_frame()] + [s.data_frame(i) for i in range(400)]
    monkeypatch.setattr(serial, "Serial", DeadSerial)
    t0 = time.time()
    real_sleep = time.sleep

    def watchdog_sleep(dt):  # a logger that ignores --duration fails here instead of hanging
        if time.time() - t0 > 8.0:
            raise RuntimeError("logger did not stop at --duration while reconnecting")
        real_sleep(dt)

    monkeypatch.setattr(logger.time, "sleep", watchdog_sleep)
    assert logger.main(["--port", "FAKE", "--out", str(tmp_path / "x.parquet"), "--duration", "2",
                        "--no-udp"]) == 0
    assert time.time() - t0 < 4.0


def test_rate_does_not_bridge_an_interpolated_gap():
    from kman_analysis import load_config
    from kman_analysis.breathing import detect_breaths

    fs = 20.0
    t = np.arange(0, 60, 1 / fs)
    wave = 0.5 * np.sin(2 * np.pi * (15 / 60) * t)
    invalid = np.zeros(len(t), dtype=bool)
    a, b = int(27 * fs), int(31 * fs) + 1
    invalid[a:b] = True
    wave[a:b] = np.linspace(wave[a], wave[b], b - a)  # what preprocess does over a long hole
    br = detect_breaths(t, wave, fs, invalid, load_config(), n_full=len(t) * 5, fs_full=100.0)
    assert len(br.rate_bpm) > 5 and np.allclose(br.rate_bpm, 15.0, atol=0.5)


def test_phase_never_valid_on_invalid_samples():
    from kman_analysis import load_config
    from kman_analysis.phase import chest_abdomen_phase

    fs = 20.0
    t = np.arange(0, 120, 1 / fs)
    chest = np.sin(2 * np.pi * 0.25 * t)
    abd = -np.sin(2 * np.pi * 0.25 * t)
    invalid = np.zeros(len(t), dtype=bool)
    invalid[int(50 * fs):int(53 * fs)] = True  # 3 s artifact inside a clean 10 s window
    calib = np.zeros(len(t), dtype=bool)
    calib[: int(30 * fs)] = True
    ph = chest_abdomen_phase(t, chest, abd, fs, calib, invalid, load_config())
    assert not (ph.valid & invalid).any()
    assert ph.valid[~invalid].mean() > 0.8


def test_report_needs_both_zoom_bounds(capsys):
    import report

    with pytest.raises(SystemExit):
        report.main(["data/sample_synthetic.parquet", "--start", "10"])
    assert "--start and --end" in capsys.readouterr().err


def test_simulator_noise_does_not_swing_at_the_ends():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(20000)
    y = sim._smooth(x, 1000)
    interior = y[2000:-2000].std()
    assert abs(y[:50]).max() < 6 * interior and abs(y[-50:]).max() < 6 * interior
