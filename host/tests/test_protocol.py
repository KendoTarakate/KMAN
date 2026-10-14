import json

import numpy as np

from kman_host import protocol as P


def test_crc_reference_vector():
    # CRC-16/CCITT-FALSE check value
    assert P.crc16(b"123456789") == 0x29B1


def test_roundtrip_and_split_chunks():
    frames = [P.encode_frame(P.TYPE_DATA, bytes(range(n))) for n in (1, 7, 30)]
    stream = b"".join(frames)
    p = P.FrameParser()
    got = []
    for b in stream:  # worst case: one byte at a time
        got += p.feed(bytes([b]))
    assert [f.raw for f in got] == frames
    assert p.stats.crc_errors == 0 and p.stats.bytes_discarded == 0


def test_resync_after_garbage_and_corruption():
    good = P.encode_frame(P.TYPE_DATA, b"\x01\x02\x03")
    bad = bytearray(good)
    bad[6] ^= 0x40
    stream = b"\x00\xaa\x13garbage\xaa" + bytes(bad) + good + b"\xaa\x55\xff\xff" + good
    p = P.FrameParser()
    got = p.feed(stream)
    assert [f.raw for f in got] == [good, good]
    assert p.stats.crc_errors == 1
    assert p.stats.bad_length == 1


def test_chunking_invariance_when_frame_ends_in_0xaa():
    # Find a body whose CRC's high byte (the frame's last byte) is 0xAA.
    body = next(bytes([i, j]) for i in range(256) for j in range(256) if P.encode_frame(1, bytes([i, j]))[-1] == 0xAA)
    fr = P.encode_frame(P.TYPE_DATA, body)
    stream = fr + P.encode_frame(P.TYPE_DATA, b"\x55\x01") + fr
    for chunk in (1, 2, 3, len(stream)):
        p = P.FrameParser()
        got = []
        for i in range(0, len(stream), chunk):
            got += p.feed(stream[i : i + chunk])
        assert len(got) == 3
        assert p.stats.bytes_discarded == 0 and p.stats.crc_errors == 0, chunk


def test_sync_bytes_inside_payload():
    body = b"\xaa\x55\xaa\x55" * 4
    fr = P.encode_frame(P.TYPE_LOG, body)
    p = P.FrameParser()
    assert [f.body for f in p.feed(fr + fr)] == [body, body]


def test_header_dtype():
    hdr = {
        "type": "kman_session_header", "sample_rate_hz": 100, "period_us": 10000, "present_mask": 5,
        "sensors": [{"slot": "chest_imu", "status": "ok"}],
        "channels": [{"name": "chest_ax", "slot": "chest_imu", "bit": 0, "unit": "g", "scale": 61e-6},
                     {"name": "flex_raw", "slot": "neck_flex", "bit": 2, "unit": "counts", "scale": 1}],
    }
    h = P.SessionHeader.from_json(json.dumps(hdr))
    dt = h.data_dtype()
    assert dt.itemsize == 7 + 4
    body = P.DATA_PREFIX.pack(5, 6, 5) + np.array([-100, 9000], "<i2").tobytes()
    rec = P.decode_data(body, dt)
    assert rec["chest_ax"][0] == -100 and rec["flex_raw"][0] == 9000
    assert P.decode_data(body + b"\x00", dt) is None
    assert h.slot_present("chest_imu") and not h.slot_present("abdomen_imu")
