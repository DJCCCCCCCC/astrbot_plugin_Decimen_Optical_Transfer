"""encoder.py — 把文件/文本编码为 decimen 兼容的喷泉帧字节流（wire v2）。

链路（与 astrbot-qrurl-plugin-design.md §4 一致，纯 Python 标准库）：
  输入(文本/文件字节) → DCF2 容器 → 切 K 块 → 喷泉帧(systematic / +repair)
  → 20B 帧头拼接 → streamBytes → base64url

硬字节约束（接收端零改动依赖这些逐位一致）：
  - 20B 小端帧头（magic 0xD1 0x0D, wire v2）
  - DCF2 容器 49B 头（'DCF2' + gzip 标志 + 长度 + SHA-256）
  - FNV-1a 容器指纹（每帧同值，参与 streamIdentity）
  - 喷泉帧块子集确定性（见 fountain_port.py）
gzip 内容仅要求"格式合法可解压"（接收端标准 gzip 解压 + ISIZE 校验），
不要求与浏览器压缩字节逐位一致。

golden 向量验证见 tests/。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import math
import re
import secrets

from fountain_port import LTEncoder, fnv1a

# --------------------------------------------------------------------------
# 线格式常量（与 shared/protocol.ts 一致）
# --------------------------------------------------------------------------
MAGIC0 = 0xD1
MAGIC1 = 0x0D  # wire v2：systematic-carousel fountain
HEADER_LEN = 20
FILE_HEADER_LEN = 49
FILE_MAGIC = b"DCF2"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BLOCKS = 0xFFFF  # k 是 u16

# snippet（shared/snippet.ts）
SNIPPET_MEDIA_TYPE = "application/vnd.decimen.snippet"
SNIPPET_FILE_NAME = "snippet.txt"
MAX_SNIPPET_BYTES = 4 * 1024 * 1024

# 每帧负载字节数（不含 20B 帧头）。
# 对齐 decimen shared/send-settings.ts 的官方档位：
#   FRAME_BYTES_OPTIONS = [500, 1000, 1465, 1850, 2331, 2953]（含 20B 帧头）
#   → block_len = 480, 980, 1445, 1830, 2311, 2933
# 默认 1445（帧字节 1465）：官方「信号不佳时降到」的均衡档，比旧默认 500
# 提升约 2.9 倍/帧（QR V25，117 模块，手机正常距离即可稳定扫描）。
DEFAULT_BLOCK_LEN = 1445

# 完整档位（帧字节 → 负载字节），供 _conf_schema 下拉与自适应选择。
BLOCK_LEN_OPTIONS: tuple[int, ...] = (480, 980, 1445, 1830, 2311, 2933)

# gzip 判定（shared/protocol.ts 的 isPrecompressedType）
_PRECOMPRESSED_TYPES = {
    "application/gzip",
    "application/java-archive",
    "application/vnd.rar",
    "application/x-7z-compressed",
    "application/x-brotli",
    "application/x-bzip",
    "application/x-bzip2",
    "application/x-gzip",
    "application/x-lzma",
    "application/x-rar-compressed",
    "application/x-xz",
    "application/x-zip-compressed",
    "application/zip",
    "application/zstd",
}
_COMPRESSIBLE_IMAGES_RE = re.compile(
    r"^image/(bmp|x-ms-bmp|svg\+xml|tiff|x-icon|vnd\.microsoft\.icon)$"
)
_COMPRESSIBLE_AUDIO_RE = re.compile(
    r"^audio/(wav|x-wav|wave|vnd\.wave|aiff|x-aiff|basic|l16)$"
)


# --------------------------------------------------------------------------
# 元数据与压缩判定
# --------------------------------------------------------------------------
def safe_file_name(name: str) -> str:
    """收窄为裸文件名，剥离路径与控制字符（shared/protocol.ts）。"""
    base = re.split(r"[\\/]", name)[-1]
    cleaned = re.sub(r"[\u0000-\u001f\u007f]", "", base).strip()
    return cleaned if cleaned not in ("", ".", "..") else "transfer.bin"


def is_precompressed_type(media_type: str) -> bool:
    """该类型是否预压缩（跳过 gzip 尝试）。"""
    media = media_type.split(";")[0].strip().lower()
    if media.startswith("video/"):
        return True
    if media.startswith("image/"):
        return not bool(_COMPRESSIBLE_IMAGES_RE.match(media))
    if media.startswith("audio/"):
        return not bool(_COMPRESSIBLE_AUDIO_RE.match(media))
    if media.startswith("application/vnd.openxmlformats-officedocument."):
        return True
    if media.startswith("application/vnd.oasis.opendocument."):
        return True
    if media.endswith("+zip"):
        return True
    return media in _PRECOMPRESSED_TYPES


# --------------------------------------------------------------------------
# DCF2 容器
# --------------------------------------------------------------------------
def pack_file(name: str, media_type: str, data: bytes) -> bytes:
    """打包 DCF2 容器（49B 头 + name + media_type + transmitted）。"""
    if len(data) == 0:
        raise ValueError("Choose a non-empty file.")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Files are limited to {MAX_FILE_BYTES // 1024 // 1024} MB.")

    name_bytes = safe_file_name(name).encode("utf-8")
    type_bytes = (media_type or "application/octet-stream").encode("utf-8")
    if len(name_bytes) > 0xFFFF or len(type_bytes) > 0xFFFF:
        raise ValueError("The file name or media type is too long.")

    # gzip 仅当真的更小（且值得尝试）才用 —— 与 decimen 同逻辑
    try_gzip = len(data) >= 768 and not is_precompressed_type(media_type)
    sha256 = hashlib.sha256(data).digest()
    compressed = gzip.compress(data) if try_gzip else None
    use_gzip = compressed is not None and len(compressed) + 64 < len(data)
    transmitted = compressed if use_gzip else data

    header = (
        FILE_MAGIC
        + bytes([1 if use_gzip else 0])
        + len(name_bytes).to_bytes(2, "little")
        + len(type_bytes).to_bytes(2, "little")
        + len(data).to_bytes(4, "little")
        + len(transmitted).to_bytes(4, "little")
        + sha256
    )
    return header + name_bytes + type_bytes + transmitted


def pack_snippet(text: str) -> bytes:
    """文本 snippet 走与文件相同的容器管线（shared/snippet.ts）。"""
    if text.strip() == "":
        raise ValueError("Paste or type some text before sending.")
    data = text.encode("utf-8")
    if len(data) > MAX_SNIPPET_BYTES:
        raise ValueError(
            f"Text snippets are limited to {MAX_SNIPPET_BYTES // 1024 // 1024} MB."
        )
    return pack_file(SNIPPET_FILE_NAME, SNIPPET_MEDIA_TYPE, data)


# --------------------------------------------------------------------------
# 帧头与喷泉流
# --------------------------------------------------------------------------
def pack_frame(
    session_id: int,
    seq: int,
    k: int,
    block_len: int,
    total_len: int,
    payload_fnv: int,
    block: bytes,
) -> bytes:
    """20 字节小端帧头 + 块（shared/protocol.ts packFrame）。"""
    head = (
        bytes([MAGIC0, MAGIC1])
        + (session_id & 0xFFFF).to_bytes(2, "little")
        + (seq & 0xFFFFFFFF).to_bytes(4, "little")
        + (k & 0xFFFF).to_bytes(2, "little")
        + (block_len & 0xFFFF).to_bytes(2, "little")
        + (total_len & 0xFFFFFFFF).to_bytes(4, "little")
        + (payload_fnv & 0xFFFFFFFF).to_bytes(4, "little")
    )
    return head + block


def encode_stream(
    container: bytes,
    block_len: int = DEFAULT_BLOCK_LEN,
    session_id: int | None = None,
    use_repair: bool = False,
) -> bytes:
    """容器 → 喷泉帧字节流。

    - systematic-only（默认）：seq 0..K-1，第 i 帧 = 第 i 个源块（零 XOR），
      体积最小 —— URL 场景字节流完整到齐，不需要 erasure 容错。
    - use_repair=True：追加 seq K..2K-1 的 mid-degree(4-24) repair 帧，
      提高"播放暂停 / 相机漏帧"鲁棒性（体积换容错）。

    帧流总长 = K * (20 + block_len)（repair 开启时再 + K 帧）。
    """
    if len(container) == 0:
        raise ValueError("empty container")
    k = max(1, math.ceil(len(container) / block_len))
    if k > MAX_SOURCE_BLOCKS:
        raise ValueError(
            f"payload too large for block_len={block_len}: k={k} exceeds {MAX_SOURCE_BLOCKS}"
        )
    if session_id is None:
        session_id = secrets.randbelow(0xFFFF) + 1  # 1..0xFFFF，与 send/main.ts 一致
    payload_fnv = fnv1a(container)

    frames: list[bytes] = []
    # systematic sweep：直接切片源块（补零到 blockLen），与 LTEncoder.encode
    # 在 seq < k 时的输出逐字节一致，但省去逐字 XOR。
    for i in range(k):
        blk = container[i * block_len : (i + 1) * block_len]
        if len(blk) < block_len:
            blk = blk.ljust(block_len, b"\x00")
        frames.append(
            pack_frame(session_id, i, k, block_len, len(container), payload_fnv, blk)
        )
    if use_repair:
        enc = LTEncoder(container, block_len, session_id)
        for seq in range(k, 2 * k):
            frames.append(
                pack_frame(
                    session_id,
                    seq,
                    k,
                    block_len,
                    len(container),
                    payload_fnv,
                    enc.encode(seq),
                )
            )
    return b"".join(frames)


def stream_to_base64url(stream: bytes) -> str:
    """喷泉帧字节流 → URL-safe base64（去 padding，可放 URL fragment）。"""
    return base64.urlsafe_b64encode(stream).decode("ascii").rstrip("=")


def encode_snippet(text: str, **kwargs) -> str:
    """文本 → base64url 帧流（插件主入口之一）。"""
    return stream_to_base64url(encode_stream(pack_snippet(text), **kwargs))


def encode_file(name: str, media_type: str, data: bytes, **kwargs) -> str:
    """文件 → base64url 帧流（插件主入口之一）。"""
    return stream_to_base64url(
        encode_stream(pack_file(name, media_type, data), **kwargs)
    )
