"""Golden-vector 验证：fountain_port.py 与 decimen shared/fountain.ts 逐位一致。

这些向量直接从 decimen tests/fountain.test.ts 摘录。任何一项失败都意味着
改变了 wire format —— 那是破坏性变更，需要帧头 magic 升版，而不是改常量。
"""

import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fountain_port import (  # noqa: E402
    LTDecoder,
    LTEncoder,
    cycle_length,
    dlog,
    fnv1a,
    frame_composition,
    frame_indices,
    repair_indices,
    soliton_cdf,
    splitmix32,
)


def _test_payload(byte_length: int) -> bytes:
    """与 decimen tests/fountain.test.ts 的 testPayload 逐位一致。"""
    return bytes((i * 37 + (i >> 8) * 11) & 0xFF for i in range(byte_length))


def _fnv_of_doubles(values):
    """Float64Array 原始字节（小端）的 FNV-1a —— 与 TS 指纹一致。"""
    return fnv1a(struct.pack(f"<{len(values)}d", *values))


def _fnv_hex(value: int) -> str:
    return f"0x{value:08x}"


# ---------------------------------------------------------------- dlog
def test_dlog_golden():
    golden = [
        (1, 0),
        (1.5, 0.4054651081081644),
        (2, 0.6931471805599453),
        (2.718281828459045, 1),
        (10, 2.3025850929940455),
        (20, 2.995732273553991),
        (200, 5.298317366548036),
        (2000, 7.600902459542082),
        (2986, 8.001689978099137),
        (44000, 10.691944912900398),
        (131070, 11.78348681061359),
    ]
    for x, expected in golden:
        got = dlog(x)
        assert got == expected, f"dlog({x}) drifted: {got!r} != {expected!r}"


def test_dlog_differs_from_math_log():
    # dlog 不可与 math.log 互换（JS 引擎间 ulp 差异是失步根源）
    import math

    differing = 0
    for k in range(2, 20000):
        for x in (k, k / 0.5):
            if dlog(x) != math.log(x):
                differing += 1
    assert differing > 0, (
        "dlog now matches math.log bit-for-bit — did it become math.log?"
    )


# ------------------------------------------------------- degree sampling
def test_soliton_cdf_fingerprints():
    # TS: fnv1a(Float64Array bytes)，k 值对应指纹
    golden = [
        (1, "0x8c6a9878"),
        (2, "0x2417b297"),
        (17, "0x2ba41e3c"),
        (179, "0xe8b6340a"),
        (716, "0x28d31438"),
        (5000, "0x357a4c9a"),
        (22000, "0xfc512a92"),
    ]
    for k, expected in golden:
        cdf = soliton_cdf(k)
        assert len(cdf) == k
        assert cdf[-1] == 1.0, f"k={k} CDF must terminate at exactly 1"
        got = _fnv_hex(_fnv_of_doubles(cdf))
        assert got == expected, (
            f"k={k} degree distribution changed: {got} != {expected}"
        )


def test_soliton_cdf_well_formed():
    for k in [1, 2, 17, 179, 716, 22000]:
        cdf = soliton_cdf(k)
        assert len(cdf) == k
        assert cdf[-1] == 1.0
        for i in range(1, k):
            assert cdf[i] >= cdf[i - 1], f"k={k} CDF not monotonic at {i}"
        assert cdf[0] > 0, f"k={k} degree 1 must have non-zero mass"


def test_frame_indices_golden():
    golden = {
        1: [[0], [0], [0], [0], [0]],
        2: [[1], [1], [1], [0], [1]],
        17: [[3, 14], [12, 0], [6, 8], [15, 16, 13], [11, 2, 16]],
        179: [[27, 39], [30, 55], [155, 125], [28, 132, 88], [39, 75, 24]],
        716: [[27, 397], [567, 592], [155, 304], [386, 311, 625], [39, 433, 382]],
    }
    seqs = [0, 1, 2, 41, 1000]
    for k, expected in golden.items():
        cdf = soliton_cdf(k)
        for i, seq in enumerate(seqs):
            got = frame_indices(k, cdf, 4242, seq)
            assert got == expected[i], (
                f"k={k} seq={seq} subset changed: {got} != {expected[i]}"
            )


def test_frame_indices_distinct_in_range():
    for k in [1, 2, 17, 179, 4096]:
        cdf = soliton_cdf(k)
        for seq in range(3000):
            idx = frame_indices(k, cdf, 9, seq)
            assert 1 <= len(idx) <= k, f"k={k} seq={seq} degree {len(idx)}"
            assert len(set(idx)) == len(idx), f"k={k} seq={seq} repeated a block index"
            for b in idx:
                assert isinstance(b, int) and 0 <= b < k, f"k={k} seq={seq} index {b}"


def test_frame_seed_mixes_session():
    cdf = soliton_cdf(179)
    a = frame_indices(179, cdf, 1, 0)
    b = frame_indices(179, cdf, 2, 0)
    assert a != b


# --------------------------------------------------- full encoder stream
def test_encoded_stream_fingerprints():
    # 端到端 pin：覆盖 dlog、solitonCdf、frameSeed、splitmix32、frameIndices、
    # 块 padding 与 XOR 顺序。64 帧流 FNV-1a（wire v2 重新录制）。
    golden = [
        (1, 64, 1, "0xf6a115c5"),
        (23, 64, 7, "0x4a5d3eaa"),
        (179, 2933, 4242, "0x54f78d05"),
        (716, 1445, 65535, "0x75b73b85"),
    ]
    for k, block_len, session_id, expected in golden:
        payload = _test_payload(k * block_len - 7)
        enc = LTEncoder(payload, block_len, session_id)
        assert enc.k == k
        stream = b"".join(enc.encode(seq) for seq in range(64))
        got = _fnv_hex(fnv1a(stream))
        assert got == expected, (
            f"stream for k={k}/{block_len}/{session_id} changed: {got} != {expected}"
        )


def test_every_frame_is_exactly_block_len():
    block_len = 1445
    payload = _test_payload(block_len * 5 + 1)
    enc = LTEncoder(payload, block_len, 3)
    assert enc.k == 6
    for seq in range(200):
        assert len(enc.encode(seq)) == block_len


# ------------------------------------------------------------ round trip
def test_round_trip_one_sweep():
    # 干净扫过一圈即零喷泉开销完成
    for byte_length, block_len in [
        (7, 2933),
        (2933, 2933),
        (50_000, 1445),
        (512 * 1024, 2933),
        (2 * 1024 * 1024, 2933),
    ]:
        payload = _test_payload(byte_length)
        enc = LTEncoder(payload, block_len, 11)
        dec = LTDecoder(enc.k, block_len, 11, byte_length)
        for seq in range(enc.k):
            dec.add_frame(seq, enc.encode(seq))
        assert dec.is_complete, f"{byte_length}B did not complete in one sweep"
        assert dec.frames_new == enc.k
        assert dec.assemble() == payload


def test_round_trip_with_loss():
    # 丢 30% 帧：花费时间，不影响正确性
    rnd = splitmix32(23)
    byte_length, block_len = 512 * 1024, 2933
    payload = _test_payload(byte_length)
    enc = LTEncoder(payload, block_len, 23)
    dec = LTDecoder(enc.k, block_len, 23, byte_length)
    seq = 0
    while not dec.is_complete and seq < enc.k * 80 + 5000:
        if rnd() * 2.0**-32 >= 0.3:
            dec.add_frame(seq, enc.encode(seq))
        seq += 1
    assert dec.is_complete
    assert dec.assemble() == payload


def test_round_trip_mid_join():
    # 接收端中途接入，无握手
    byte_length, block_len = 512 * 1024, 2933
    payload = _test_payload(byte_length)
    enc = LTEncoder(payload, block_len, 91)
    dec = LTDecoder(enc.k, block_len, 91, byte_length)
    start = enc.k // 3
    seq = start
    while not dec.is_complete and seq < start + enc.k * 4:
        dec.add_frame(seq, enc.encode(seq))
        seq += 1
    assert dec.is_complete
    assert dec.assemble() == payload


def test_round_trip_any_order():
    byte_length, block_len = 200_000, 1445
    payload = _test_payload(byte_length)
    enc = LTEncoder(payload, block_len, 77)
    captured = [(seq, enc.encode(seq)) for seq in range(enc.k * 2 + enc.k // 2)]
    rnd = random.Random(5)
    rnd.shuffle(captured)
    dec = LTDecoder(enc.k, block_len, 77, byte_length)
    for seq, block in captured:
        dec.add_frame(seq, block)
        if dec.is_complete:
            break
    assert dec.is_complete
    assert dec.assemble() == payload


def test_single_block_payload():
    payload = _test_payload(900)
    enc = LTEncoder(payload, 2933, 5)
    assert enc.k == 1
    dec = LTDecoder(1, 2933, 5, 900)
    dec.add_frame(0, enc.encode(0))
    assert dec.is_complete
    assert dec.assemble() == payload


def test_incomplete_decoder_assembles_nothing():
    enc = LTEncoder(_test_payload(50_000), 1445, 13)
    dec = LTDecoder(enc.k, 1445, 13, 50_000)
    dec.add_frame(0, enc.encode(0))
    assert not dec.is_complete
    assert dec.assemble() is None


def test_repeated_frames_count_but_never_corrupt():
    byte_length, block_len = 60_000, 1445
    payload = _test_payload(byte_length)
    enc = LTEncoder(payload, block_len, 31)
    dec = LTDecoder(enc.k, block_len, 31, byte_length)
    seq = 0
    while not dec.is_complete:
        block = enc.encode(seq)
        dec.add_frame(seq, block)
        dec.add_frame(seq, block)  # 相机重读同一屏幕帧
        seq += 1
    assert dec.frames_dup >= dec.frames_new - 1
    assert dec.assemble() == payload


def test_redundant_frames_accounting():
    block_len = 64
    payload = _test_payload(23 * block_len - 7)
    enc = LTEncoder(payload, block_len, 77)
    dec = LTDecoder(enc.k, block_len, 77, len(payload))
    dec.add_frame(0, enc.encode(0))
    assert dec.solved_count == 1
    assert dec.frames_redundant == 0
    next_cycle = cycle_length(enc.k)
    dec.add_frame(next_cycle, enc.encode(next_cycle))
    assert dec.frames_new == 2
    assert dec.frames_dup == 0
    assert dec.frames_redundant == 1
    assert dec.solved_count == 1
    dec.add_frame(1, enc.encode(1))
    assert dec.frames_redundant == 1
    assert dec.solved_count == 2


# ------------------------------------------------------------ carousel v2
def test_carousel_composition():
    for k in [1, 17, 179, 4096]:
        assert cycle_length(k) == 2 * k
        for pos in {0, k >> 1, k - 1}:
            assert frame_composition(k, 9, pos) == [pos], f"k={k} sweep pos={pos}"
            assert frame_composition(k, 9, pos + 6 * cycle_length(k)) == [pos]
        for seq in [k, k + 1, 2 * k - 1]:
            idx = frame_composition(k, 9, seq)
            assert min(k, 4) <= len(idx) <= min(k, 24), (
                f"k={k} seq={seq} degree {len(idx)}"
            )
            assert len(set(idx)) == len(idx)


def test_repair_indices_range():
    for k in [17, 179, 4096]:
        for seq in range(k, 2 * k):
            idx = repair_indices(k, 9, seq)
            assert min(k, 4) <= len(idx) <= min(k, 24)


def test_splitmix32_deterministic():
    rnd = splitmix32(4242)
    first = rnd()
    rnd2 = splitmix32(4242)
    assert rnd2() == first
    assert rnd() != first  # 序列前进


def _run_all():
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
