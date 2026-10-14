"""Decode frames produced by the firmware's own C++ encoder (compiled natively)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kman_host import protocol as P

FW = Path(__file__).resolve().parents[2] / "firmware"
CXX = shutil.which("g++") or shutil.which("clang++")


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    if not CXX:
        pytest.skip("no native C++ compiler")
    exe = tmp_path_factory.mktemp("native") / "native_frames"
    subprocess.run(
        [CXX, "-std=c++11", "-Wall", "-Wextra", "-Werror", f"-I{FW / 'src'}",
         str(FW / "test" / "native_frames.cpp"), str(FW / "src" / "protocol.cpp"), "-o", str(exe)],
        check=True,
    )
    out = subprocess.run([str(exe)], check=True, capture_output=True).stdout
    parser = P.FrameParser()
    got = parser.feed(out)
    assert parser.stats.crc_errors == 0
    return got


def test_data_frame(frames):
    f = frames[0]
    assert f.type == P.TYPE_DATA
    t, seq, valid = P.DATA_PREFIX.unpack_from(f.body)
    assert (t, seq, valid) == (0xFFFFFF00, 0xFFFE, 0x05)
    import numpy as np

    ch = np.frombuffer(f.body[7:], dtype="<i2").tolist()
    assert ch == [-32768, 32767, 16393, -1, 0, 1234, 8191]


def test_status_frame(frames):
    st = P.decode_status(frames[1].body)
    assert st is not None
    assert st.t_us == 123456789 and st.ticks == 4000000000
    assert (st.frames_sent, st.overruns, st.tx_drops) == (42, 3, 7)
    assert st.read_errors == (1, 0, 0, 9)
    assert (st.max_latency_us, st.tx_high_water) == (650, 280)
    assert st.hfxo and not st.sensor_error


def test_json_writer(frames):
    d = json.loads(frames[2].body)
    assert d["quote"] == 'a"b\\c\n'
    assert d["scale"] == pytest.approx(61e-6)
    assert d["lpf"] == pytest.approx(20.8)
    assert d["int"] == 7
    assert d["addr"] == "0x6A"
    assert d["neg"] == -5 and d["big"] == 4000000000 and d["flag"] is True
    assert d["list"] == ["x", {"name": "chest_ax"}]


def test_json_overflow_detected(frames):
    assert frames[3].body == b"overflow-detected"


def test_python_encoder_matches_cpp(frames):
    for f in frames:
        assert P.encode_frame(f.type, f.body) == f.raw
