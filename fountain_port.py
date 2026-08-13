"""fountain_port.py — decimen-optical-transfer 喷泉码（wire v2）的 Python 移植。

逐位对齐 shared/fountain.ts + shared/protocol.ts 中的确定性算法：
splitmix32 / fnv1a / dlog / solitonCdf / frameIndices / frameComposition /
repairIndices / LTEncoder / LTDecoder。

为什么必须逐位一致：
  发送端与接收端各自独立推导每一帧的块子集，永不互通。任何一位漂移
  都会让收发静默失步（传输永不完成）。dlog() 不能用 math.log 替代——
  JS 引擎间 Math.log 就有 ulp 差异（详见 decimen docs/technical/protocol.md）。

golden 向量验证见 tests/test_fountain.py。
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------
# 常量（与 TS 源码一致）
# --------------------------------------------------------------------------
LN2 = 0.6931471805599453
SOLITON_C = 0.1
SOLITON_DELTA = 0.5
REPAIR_DEGREE_MIN = 4
REPAIR_DEGREE_MAX = 24

_MASK32 = 0xFFFFFFFF


def _mul32(a: int, b: int) -> int:
    """Math.imul 的 Python 等价：32 位乘法取低 32 位。"""
    return (a * b) & _MASK32


# --------------------------------------------------------------------------
# splitmix32 / fnv1a（shared/protocol.ts）
# --------------------------------------------------------------------------
def splitmix32(seed: int):
    """确定性 PRNG，JS 引擎间完全一致（纯整数运算）。

    返回一个无参函数，每次调用返回下一个 u32。
    """
    s = seed & _MASK32

    def gen() -> int:
        nonlocal s
        s = (s + 0x9E3779B9) & _MASK32
        t = s ^ (s >> 16)
        t = _mul32(t, 0x21F0AAAD)
        t ^= t >> 15
        t = _mul32(t, 0x735A2D97)
        t ^= t >> 15
        return t & _MASK32

    return gen


def fnv1a(data: bytes) -> int:
    """FNV-1a 32 位，u32 截断（shared/protocol.ts）。"""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = _mul32(h, 0x01000193)
    return h


# --------------------------------------------------------------------------
# dlog（shared/fountain.ts）— IEEE-754 精确对数，不可替换为 math.log
# --------------------------------------------------------------------------
def dlog(x: float) -> float:
    """确定性自然对数：精确运算的 range reduction + atanh 级数。

    与 Math.log 至多差 1 ulp（约 1/4 输入），足以移动 CDF 边界、翻转
    采样的 degree。换成 math.log 会失步。
    """
    e = 0
    m = x
    while m >= 1.5:
        m /= 2.0
        e += 1
    while m < 0.75:
        m *= 2.0
        e -= 1
    z = (m - 1.0) / (m + 1.0)
    z2 = z * z
    term = z
    total = 0.0
    for n in range(1, 22, 2):  # n = 1,3,5,...,21（11 项，与 TS 一致）
        total += term / n
        term *= z2
    return e * LN2 + 2.0 * total


# --------------------------------------------------------------------------
# v1 soliton（保留：golden 向量背书，未来格式可能复用）
# --------------------------------------------------------------------------
def soliton_cdf(k: int) -> list[float]:
    """Robust-soliton degree CDF（k 个源块）。返回长度 k 的 float 列表。"""
    cdf = [0.0] * k
    if k == 1:
        cdf[0] = 1.0
        return cdf
    R = max(1.0, SOLITON_C * dlog(k / SOLITON_DELTA) * math.sqrt(k))
    spike = min(k, math.ceil(k / R))
    total = 0.0
    for d in range(1, k + 1):
        rho = 1.0 / k if d == 1 else 1.0 / (d * (d - 1))
        tau = 0.0
        if d < spike:
            tau = R / (d * k)
        elif d == spike:
            tau = (R * max(0.0, dlog(R / SOLITON_DELTA))) / k
        total += rho + tau
        cdf[d - 1] = total
    for i in range(k):
        cdf[i] = cdf[i] / total
    cdf[k - 1] = 1.0
    return cdf


def _frame_seed(session_id: int, seq: int) -> int:
    h = (_mul32(session_id + 1, 0x9E3779B1) ^ (seq + 0x85EBCA6B)) & _MASK32
    h = _mul32(h ^ (h >> 13), 0xC2B2AE35)
    return (h ^ (h >> 16)) & _MASK32


def frame_indices(k: int, cdf: list[float], session_id: int, seq: int) -> list[int]:
    """帧 seq 异或进去的块下标（v1 soliton 流；wire v2 不再发射，但保留）。

    注意：JS 的 [...Set] 按插入顺序输出，因此这里必须用 list + seen 保持
    与 TS 完全一致的顺序（顺序影响后续 XOR 与指纹）。
    """
    rnd = splitmix32(_frame_seed(session_id, seq))
    u = rnd() * 2.0**-32
    lo, hi = 0, k - 1
    while lo < hi:
        mid = (lo + hi) >> 1
        if cdf[mid] >= u:
            hi = mid
        else:
            lo = mid + 1
    d = min(k, lo + 1)
    if d > (k >> 3):
        # 大 degree：部分 Fisher-Yates（与 TS 的 scratch 数组一致）
        scratch = list(range(k))
        out: list[int] = []
        for i in range(d):
            j = i + (rnd() % (k - i))
            scratch[i], scratch[j] = scratch[j], scratch[i]
            out.append(scratch[i])
        return out
    # 小 degree：Set 语义（去重、保插入顺序）
    out = []
    seen: set[int] = set()
    while len(out) < d:
        v = rnd() % k
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# --------------------------------------------------------------------------
# wire v2：systematic carousel（shared/fountain.ts）
# --------------------------------------------------------------------------
def cycle_length(k: int) -> int:
    """每个 carousel 周期帧数：一次 systematic sweep + k 个 repair 帧。"""
    return 2 * k


def repair_indices(k: int, session_id: int, seq: int) -> list[int]:
    """repair 帧：uniform mid-degree（4–24），绝对 seq 播种。"""
    rnd = splitmix32(_frame_seed(session_id, seq))
    d = min(
        k, REPAIR_DEGREE_MIN + (rnd() % (REPAIR_DEGREE_MAX - REPAIR_DEGREE_MIN + 1))
    )
    out: list[int] = []
    seen: set[int] = set()
    while len(out) < d:
        v = rnd() % k
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def frame_composition(k: int, session_id: int, seq: int) -> list[int]:
    """帧 seq 的块子集：sweep 期 systematic，之后 mid-degree repair。"""
    pos = seq % cycle_length(k)
    return [pos] if pos < k else repair_indices(k, session_id, seq)


class LTEncoder:
    """LT 编码器（wire v2）。systematic 帧输出 == 源块（补零到 blockLen）。"""

    def __init__(self, payload: bytes, block_len: int, session_id: int):
        if block_len <= 0:
            raise ValueError("block_len must be positive")
        self.block_len = block_len
        self.session_id = session_id
        self.k = max(1, math.ceil(len(payload) / block_len))
        self.words = math.ceil(block_len / 4)
        # 每块按小端 u32 存储，尾块不足部分补零（与 Uint32Array 一致）
        buf = bytearray(self.k * self.words * 4)
        for b in range(self.k):
            src = payload[b * block_len : min((b + 1) * block_len, len(payload))]
            buf[b * self.words * 4 : b * self.words * 4 + len(src)] = src
        self._blocks = [
            int.from_bytes(buf[o : o + 4], "little") for o in range(0, len(buf), 4)
        ]

    def encode(self, seq: int) -> bytes:
        idx = frame_composition(self.k, self.session_id, seq)
        out = bytearray(self.words * 4)
        for b in idx:
            off = b * self.words
            for w in range(self.words):
                o = w * 4
                v = int.from_bytes(out[o : o + 4], "little") ^ self._blocks[off + w]
                out[o : o + 4] = v.to_bytes(4, "little")
        return bytes(out[: self.block_len])


class _PendingFrame:
    """解码器中待求解的中间帧（块下标集合 + 当前 XOR 值）。"""

    __slots__ = ("idx", "words")

    def __init__(self, idx: set[int], words: bytearray):
        self.idx = idx
        self.words = words


class LTDecoder:
    """LT 解码器（wire v2）：剥皮级联，任意顺序/任意 ~K 个不同帧可还原。"""

    def __init__(self, k: int, block_len: int, session_id: int, total_len: int):
        self.k = k
        self.block_len = block_len
        self.session_id = session_id
        self.total_len = total_len
        self.words = math.ceil(block_len / 4)
        self._solved: list[bytes | None] = [None] * k
        self._by_block: dict[int, list[_PendingFrame]] = {}
        self._seen: set[int] = set()
        self.solved_count = 0
        self.frames_new = 0
        self.frames_dup = 0
        self.frames_redundant = 0

    @property
    def is_complete(self) -> bool:
        return self.solved_count >= self.k

    def add_frame(self, seq: int, block: bytes) -> None:
        if seq in self._seen:
            self.frames_dup += 1
            return
        self._seen.add(seq)
        self.frames_new += 1
        if self.is_complete:
            return

        idx = set(frame_composition(self.k, self.session_id, seq))
        words = bytearray(self.words * 4)
        take = min(self.block_len, len(block))
        words[:take] = block[:take]
        for b in list(idx):
            s = self._solved[b]
            if s is not None:
                self._xor_into(words, s)
                idx.discard(b)
        if not idx:
            self.frames_redundant += 1
            return
        if len(idx) == 1:
            self._resolve(next(iter(idx)), words)
            return
        pf = _PendingFrame(idx, words)
        for b in idx:
            self._by_block.setdefault(b, []).append(pf)

    def _xor_into(self, dst: bytearray, src: bytes) -> None:
        for w in range(self.words):
            o = w * 4
            v = int.from_bytes(dst[o : o + 4], "little") ^ int.from_bytes(
                src[o : o + 4], "little"
            )
            dst[o : o + 4] = v.to_bytes(4, "little")

    def _resolve(self, b0: int, w0: bytearray) -> None:
        queue: list[tuple[int, bytearray]] = [(b0, w0)]
        while queue:
            b, w = queue.pop()
            if self._solved[b] is not None:
                continue
            self._solved[b] = bytes(w)
            self.solved_count += 1
            waiting = self._by_block.pop(b, None)
            if not waiting:
                continue
            for pf in waiting:
                self._xor_into(pf.words, self._solved[b])
                pf.idx.discard(b)
                if len(pf.idx) == 1:
                    r = next(iter(pf.idx))
                    if self._by_block.get(r) is not None:
                        lst = self._by_block[r]
                        if pf in lst:
                            lst.remove(pf)
                            if not lst:
                                del self._by_block[r]
                    if self._solved[r] is None:
                        queue.append((r, pf.words))

    def assemble(self) -> bytes | None:
        if not self.is_complete:
            return None
        out = bytearray(self.total_len)
        for b in range(self.k):
            start = b * self.block_len
            ln = min(self.block_len, self.total_len - start)
            if ln > 0:
                out[start : start + ln] = self._solved[b][:ln]
        return bytes(out)
