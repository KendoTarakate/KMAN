"""Analysis checks against simulator ground truth, mirroring the Step 1
acceptance criteria: rest breathing vs breath-hold, swallows vs head movement,
normal vs simulated obstruction (with abdomen IMU)."""

import numpy as np
import pytest
from scipy import signal

from kman_analysis import analyze, load_config
from kman_analysis.breathing import gravity_complementary
from kman_host import simulator as sim
from kman_host.session_io import SessionWriter, load_session
from kman_host.stream import StreamDecoder


def _session(tmp_path, name, cfg=None, **stream_kw):
    s = sim.generate(cfg or sim.SimConfig())
    dec = StreamDecoder()
    dec.schedule_labels(s.label_events())
    w = SessionWriter(tmp_path / f"{name}.parquet", raw=False)
    for _, fr in s.stream(**stream_kw):
        dec.feed(fr, 0.0)
        if dec.new_header:
            w.set_header(dec.header)
            dec.new_header = False
    w.write_batch(dec.drain())
    w.close(dec.headers, dec.label_events_dicts(), dec.summary())
    return s, load_session(tmp_path / f"{name}.parquet")


@pytest.fixture(scope="module")
def abd(tmp_path_factory):
    s, sess = _session(tmp_path_factory.mktemp("abd"), "abd", sim.SimConfig(abdomen=True))
    return s, analyze(sess, load_config())


def row(a, label):
    return a.summary.set_index("label").loc[label]


def test_rest_breathing_rate_and_detection(abd):
    s, a = abd
    assert row(a, 1)["rate_median_bpm"] == pytest.approx(14.0, abs=1.0)
    resp = s.truth["resp"]
    pk, _ = signal.find_peaks(resp, prominence=0.1 * resp.std())
    truth_t = pk / s.cfg.fs
    rest = truth_t[s.labels[pk] == 1]
    det = a.chest.breath_t[a.chest.breath_valid]
    matched = np.mean([np.min(np.abs(det - x)) < 1.0 for x in rest])
    assert matched > 0.95


def test_rest_vs_breath_hold(abd):
    _, a = abd
    rest, hold = row(a, 1), row(a, 8)
    assert hold["max_breath_gap_s"] > 15
    assert rest["max_breath_gap_s"] < 8
    assert hold["breaths_per_min"] < 5 < 10 < rest["breaths_per_min"]


def test_swallow_vs_head_movement(abd):
    s, a = abd
    dry, head = row(a, 4), row(a, 5)
    # dry-swallow segment: swallows found, no head events
    n_truth = int(np.sum(s.labels[(s.truth["swallow_times"] * s.cfg.fs).astype(int)] == 4))
    assert dry["swallows"] >= 0.8 * n_truth
    assert dry["head_events"] == 0
    # head-movement segment: head events found, no swallows
    assert head["head_events"] >= 0.8 * len(s.truth["head_times"])
    assert head["swallows"] == 0
    # detected swallow peaks sit on true swallows
    ev = a.neck.events
    sw = ev.loc[(ev["kind"] == "swallow") & (ev["label"] == 4), "peak_s"].to_numpy()
    assert all(np.min(np.abs(s.truth["swallow_times"] - t)) < 1.5 for t in sw)


def test_normal_vs_obstruction_phase(abd):
    _, a = abd
    rest, obs = row(a, 1), row(a, 9)
    assert abs(rest["phase_mean_deg"]) < 30 and rest["paradox_pct"] < 5
    assert abs(obs["phase_mean_deg"]) > 150 and obs["paradox_pct"] > 80


def test_motion_artifact_flag(abd):
    _, a = abd
    assert row(a, 6)["artifact_pct"] > 90  # walking
    assert row(a, 1)["artifact_pct"] < 5
    assert row(a, 2)["artifact_pct"] < 20  # talking is not motion


def test_robust_to_frame_loss(tmp_path):
    s, sess = _session(tmp_path, "lossy", drop_prob=0.01, corrupt_prob=0.002, seed=4)
    a = analyze(sess, load_config())
    assert a.qa["lost_pct"] > 0.5
    assert row(a, 1)["rate_median_bpm"] == pytest.approx(14.0, abs=1.0)
    assert row(a, 8)["max_breath_gap_s"] > 15


def test_long_gap_is_masked(tmp_path):
    s = sim.generate(sim.SimConfig())
    dec = StreamDecoder()
    dec.schedule_labels(s.label_events())
    w = SessionWriter(tmp_path / "gap.parquet", raw=False)
    for i, fr in s.stream():
        if 3000 <= i < 3300:  # 3 s outage inside rest breathing
            continue
        dec.feed(fr, 0.0)
        if dec.new_header:
            w.set_header(dec.header)
            dec.new_header = False
    w.write_batch(dec.drain())
    w.close(dec.headers, dec.label_events_dicts(), dec.summary())
    a = analyze(load_session(tmp_path / "gap.parquet"), load_config())
    assert a.u.gap[3000:3300].all() and not a.u.gap[:2990].any()
    bt = a.chest.breath_t
    inside = (bt >= 30) & (bt < 33)
    assert not a.chest.breath_valid[inside].any()


def test_neck_imu_flags_head_motion(tmp_path):
    s, sess = _session(tmp_path, "neck", sim.SimConfig(neck_imu=True))
    a = analyze(sess, load_config(overrides={"swallow": {"head_amplitude_k": 1e9, "max_duration_s": 1e9}}))
    ev = a.neck.events
    head = ev[ev["label"] == 5]
    assert (head["kind"] == "head_motion").mean() > 0.8
    assert head["reason"].str.startswith("neck imu").all()
    dry = ev[ev["label"] == 4]
    assert (dry["kind"] == "swallow").all()


def test_complementary_filter_tracks_rotation():
    fs = 100.0
    t = np.arange(0, 20, 1 / fs)
    th = np.radians(5) * np.sin(2 * np.pi * 0.3 * t)  # body rotation about x
    up = np.array([0.0, 1.0, 0.0])
    g = np.stack([np.zeros_like(t), np.cos(-th) * up[1], np.sin(-th) * up[1]], axis=1)
    rng = np.random.default_rng(0)
    acc = g + rng.standard_normal(g.shape) * 0.01  # noisy accel
    gyr = np.stack([np.degrees(np.gradient(th) * fs), np.zeros_like(t), np.zeros_like(t)], axis=1)
    est = gravity_complementary(acc, gyr, fs, tau_s=1.0)
    err = np.degrees(np.abs(np.arctan2(est[:, 2], est[:, 1]) - (-th)))[200:]
    acc_err = np.degrees(np.abs(np.arctan2(acc[:, 2], acc[:, 1]) - (-th)))[200:]
    assert err.max() < 0.5
    assert err.std() < 0.3 * acc_err.std()  # fusion beats raw accel tilt


def test_config_rejects_unknown_keys():
    with pytest.raises(KeyError):
        load_config(overrides={"swallow": {"k_onn": 3}})
