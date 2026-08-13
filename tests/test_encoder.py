"""Golden-vector 验证：encoder.py 的容器/帧头/流结构与 decimen 线格式一致。

向量来源：decimen tests/protocol.test.ts 与设计文档 §7 线格式规格。
"""

import gzip
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from encoder import (  # noqa: E402
    DEFAULT_BLOCK_LEN,
    HEADER_LEN,
    MAGIC0,
    MAGIC1,
    SNIPPET_FILE_NAME,
    SNIPPET_MEDIA_TYPE,
    encode_file,
    encode_snippet,
    encode_stream,
    fnv1a,
    is_precompressed_type,
    pack_file,
    pack_frame,
    pack_snippet,
    safe_file_name,
    stream_to_base64url,
)
from fountain_port import LTDecoder  # noqa: E402

FILE_HEADER_LEN = 49
FILE_MAGIC = b"DCF2"


# ---------------------------------------------------------------- 帧头
def test_pack_frame_bytes_golden():
    # decimen tests/protocol.test.ts 的字节级期望
    frame = pack_frame(
        session_id=0xBEEF,
        seq=0x01020304,
        k=0x0111,
        block_len=6,
        total_len=0x00FEDCBA,
        payload_fnv=0x89ABCDEF,
        block=bytes([1, 2, 3, 4, 5, 6]),
    )
    assert (
        frame.hex(" ")
        == "d1 0d ef be 04 03 02 01 11 01 06 00 ba dc fe 00 ef cd ab 89 01 02 03 04 05 06"
    )
    assert len(frame) == HEADER_LEN + 6


def test_pack_frame_little_endian_fields():
    frame = pack_frame(0x1234, 5, 3, 500, 1500, 0xCAFEBABE, b"\x00" * 500)
    assert frame[0] == MAGIC0 and frame[1] == MAGIC1
    assert struct.unpack_from("<H", frame, 2)[0] == 0x1234
    assert struct.unpack_from("<I", frame, 4)[0] == 5
    assert struct.unpack_from("<H", frame, 8)[0] == 3
    assert struct.unpack_from("<H", frame, 10)[0] == 500
    assert struct.unpack_from("<I", frame, 12)[0] == 1500
    assert struct.unpack_from("<I", frame, 16)[0] == 0xCAFEBABE
    assert len(frame) == 20 + 500


# ---------------------------------------------------------------- DCF2 容器
def _unpack(container: bytes) -> dict:
    """最小 unpack（对照 decimen unpackFile 的关键校验）。"""
    assert container[:4] == FILE_MAGIC, "container magic"
    gzip_flag = container[4]
    name_len, type_len = struct.unpack_from("<HH", container, 5)
    orig_len, trans_len = struct.unpack_from("<II", container, 9)
    sha = container[17:49]
    data_offset = FILE_HEADER_LEN + name_len + type_len
    assert data_offset + trans_len == len(container), "length mismatch"
    transmitted = container[data_offset:]
    return {
        "gzip": gzip_flag == 1,
        "name": container[FILE_HEADER_LEN : FILE_HEADER_LEN + name_len].decode("utf-8"),
        "type": container[data_offset - type_len : data_offset].decode("utf-8"),
        "orig_len": orig_len,
        "trans_len": trans_len,
        "sha256": sha,
        "transmitted": transmitted,
    }


def test_pack_file_container_structure():
    data = bytes([0, 1, 2, 127, 128, 254, 255])
    container = pack_file("résumé.bin", "application/octet-stream", data)
    info = _unpack(container)
    assert info["gzip"] is False
    assert info["name"] == "résumé.bin"
    assert info["type"] == "application/octet-stream"
    assert info["orig_len"] == len(data)
    assert info["trans_len"] == len(data)
    assert info["sha256"] == hashlib.sha256(data).digest()
    assert info["transmitted"] == data


def test_pack_file_filename_sanitised():
    cases = [
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\drivers\\etc\\hosts", "hosts"),
        ("évidence.pdf", "évidence.pdf"),
        ("report v2 (final).tar.gz", "report v2 (final).tar.gz"),
    ]
    for sent, expected in cases:
        container = pack_file(sent, "application/octet-stream", b"\x01\x02\x03")
        assert _unpack(container)["name"] == expected, f"for {sent!r}"


def test_safe_file_name_fallbacks():
    for sent in ["..", ".", "/", "   ", "\u0000\u0007"]:
        assert safe_file_name(sent) == "transfer.bin", f"for {sent!r}"


def test_compressible_file_uses_gzip():
    source = ("decimen optical transfer\n" * 4000).encode("utf-8")
    container = pack_file("notes.txt", "text/plain", source)
    info = _unpack(container)
    assert info["gzip"] is True
    assert info["trans_len"] < len(source) // 10
    inflated = gzip.decompress(info["transmitted"])
    assert inflated == source
    assert info["sha256"] == hashlib.sha256(source).digest()


def test_precompressed_file_verbatim():
    source = bytes((i * 2654435761 >> 24) & 0xFF for i in range(4096))
    container = pack_file("photo.jpg", "image/jpeg", source)
    info = _unpack(container)
    assert info["gzip"] is False
    assert info["transmitted"] == source


def test_is_precompressed_type_skips_gzip():
    for t in [
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/avif",
        "image/heic",
        "video/mp4",
        "video/quicktime",
        "audio/mpeg",
        "audio/mp4",
        "audio/flac",
        "application/zip",
        "application/gzip",
        "application/x-7z-compressed",
        "application/vnd.rar",
        "application/epub+zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.oasis.opendocument.spreadsheet",
        "IMAGE/JPEG",
        "image/jpeg; charset=binary",
    ]:
        assert is_precompressed_type(t) is True, f"{t} should skip gzip"


def test_is_precompressed_type_tries_gzip():
    for t in [
        "text/plain",
        "text/csv",
        "application/json",
        "application/pdf",
        "application/wasm",
        "application/octet-stream",
        "application/vnd.decimen.snippet",
        "image/svg+xml",
        "image/bmp",
        "image/tiff",
        "image/x-icon",
        "audio/wav",
        "audio/x-aiff",
        "",
    ]:
        assert is_precompressed_type(t) is False, f"{t} should still try gzip"


def test_pack_snippet_semantics():
    container = pack_snippet("hello 世界")
    info = _unpack(container)
    assert info["name"] == SNIPPET_FILE_NAME
    assert info["type"] == SNIPPET_MEDIA_TYPE
    assert info["transmitted"] == "hello 世界".encode()


def test_pack_snippet_empty_rejected():
    try:
        pack_snippet("   ")
        assert False, "empty snippet should raise"
    except ValueError:
        pass


# ---------------------------------------------------------------- 流
def test_encode_stream_systematic_structure():
    container = pack_file("hello.txt", "text/plain", b"hello world")
    k = (len(container) + DEFAULT_BLOCK_LEN - 1) // DEFAULT_BLOCK_LEN
    stream = encode_stream(container, block_len=DEFAULT_BLOCK_LEN, session_id=7)
    assert len(stream) == k * (HEADER_LEN + DEFAULT_BLOCK_LEN)
    # 第 i 帧 = 第 i 块（补零），帧头字段正确
    for i in range(k):
        frame = stream[
            i * (20 + DEFAULT_BLOCK_LEN) : (i + 1) * (20 + DEFAULT_BLOCK_LEN)
        ]
        assert frame[0] == MAGIC0 and frame[1] == MAGIC1
        assert struct.unpack_from("<H", frame, 2)[0] == 7
        assert struct.unpack_from("<I", frame, 4)[0] == i
        assert struct.unpack_from("<H", frame, 8)[0] == k
        assert struct.unpack_from("<H", frame, 10)[0] == DEFAULT_BLOCK_LEN
        assert struct.unpack_from("<I", frame, 12)[0] == len(container)
        assert struct.unpack_from("<I", frame, 16)[0] == fnv1a(container)
        expect_block = container[i * DEFAULT_BLOCK_LEN : (i + 1) * DEFAULT_BLOCK_LEN]
        assert frame[20:] == expect_block.ljust(DEFAULT_BLOCK_LEN, b"\x00")


def test_encode_stream_round_trip():
    # 完整链路：pack_file → encode_stream → 逐帧 parse → LTDecoder → assemble
    source = bytes(range(256)) * 8  # 2048 字节，可压可不压无所谓
    container = pack_file("blob.bin", "application/octet-stream", source)
    stream = encode_stream(container, block_len=500, session_id=4242)
    k = (len(container) + 499) // 500
    frames = [stream[i * 520 : (i + 1) * 520] for i in range(k)]
    dec = LTDecoder(k, 500, 4242, len(container))
    for i, fr in enumerate(frames):
        assert fr[0] == MAGIC0 and fr[1] == MAGIC1
        assert struct.unpack_from("<I", fr, 4)[0] == i
        dec.add_frame(i, fr[20:])
    assert dec.is_complete
    assert dec.assemble() == container


def test_use_repair_round_trip():
    source = b"A" * 1200
    container = pack_file("a.txt", "text/plain", source)
    stream = encode_stream(container, block_len=500, session_id=9, use_repair=True)
    k = (len(container) + 499) // 500
    assert len(stream) == 2 * k * 520
    # 只喂 repair 帧（seq k..2k-1）也应能解出
    dec = LTDecoder(k, 500, 9, len(container))
    for seq in range(k, 2 * k):
        fr = stream[(seq - k + k) * 520 : (seq - k + k + 1) * 520]
        dec.add_frame(seq, fr[20:])
    assert dec.is_complete
    assert dec.assemble() == container


def test_base64url_no_padding():
    stream = encode_stream(
        pack_file("x.bin", "application/octet-stream", b"\x00\x01\x02"),
        block_len=500,
        session_id=1,
    )
    b64 = stream_to_base64url(stream)
    assert "=" not in b64
    import base64

    assert base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)) == stream


def test_block_too_small_raises():
    # k 超过 u16 上限必须报错（用真随机数据避免被 gzip 缩小）
    import os as _os

    noisy = _os.urandom(65536)
    container = pack_file("big.bin", "application/octet-stream", noisy)
    assert len(container) > 65535
    try:
        encode_stream(container, block_len=1, session_id=1)
        assert False, "should raise for k > 0xFFFF"
    except ValueError:
        pass


def test_high_level_entries():
    b64 = encode_snippet("hello", block_len=500, session_id=1)
    assert isinstance(b64, str) and b64
    b64f = encode_file("n.txt", "text/plain", b"data", block_len=500, session_id=1)
    assert isinstance(b64f, str) and b64f


def test_pack_file_empty_rejected():
    try:
        pack_file("e.bin", "application/octet-stream", b"")
        assert False, "empty file should raise"
    except ValueError:
        pass


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
