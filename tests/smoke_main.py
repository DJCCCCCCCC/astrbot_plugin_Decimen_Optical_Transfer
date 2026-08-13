"""main.py 业务逻辑 smoke 测试（用假 astrbot 模块隔离，不依赖真实 AstrBot 环境）。"""

import asyncio
import base64
import os
import pathlib
import struct
import sys
import tempfile
import types
import unittest.mock as um

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 注入假 astrbot 模块 ----
import tempfile as _tempfile

_DATA_DIR = _tempfile.mkdtemp(prefix="astrbot-smoke-")

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
star = types.ModuleType("astrbot.api.star")
event_m = types.ModuleType("astrbot.api.event")
comp_m = types.ModuleType("astrbot.api.message_components")


class _FakeStar:
    def __init__(self, context):
        self.context = context


star.Star = _FakeStar
star.Context = object
star.StarTools = types.SimpleNamespace(get_data_dir=lambda: pathlib.Path(_DATA_DIR))
api.logger = um.MagicMock()
event_m.AstrMessageEvent = object
event_m.filter = types.SimpleNamespace(command=lambda n: lambda f: f)


class File:
    """与 AstrBot v4.27.2 的 Comp.File(name, file) 签名一致（显示名在前）。"""

    def __init__(self, name="", file=""):
        self.name, self.file = name, file


class Image:
    def __init__(self, name="", url="", file=""):
        self.name, self.url, self.file = name, url, file


class Plain:
    def __init__(self, text=""):
        self.text = text


comp_m.File = File
comp_m.Image = Image
comp_m.Plain = Plain
sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.event": event_m,
        "astrbot.api.message_components": comp_m,
    }
)

import encoder  # noqa: E402
from fountain_port import LTDecoder  # noqa: E402

import main  # noqa: E402

Comp = comp_m  # 用例别名


async def main_test():
    plg = main.DecimenOpticalTransfer(
        None, config={"host": "https://qr.example.com", "mode": "hosted"}
    )

    # 1) snippet 路径
    b64 = plg._encode_to_b64(encoder.pack_snippet("hello 光学传输"))
    print("snippet b64 len:", len(b64))
    assert isinstance(b64, str) and not b64.endswith("=")

    # 2) file 路径（本地文件组件）
    payload = bytes(range(256)) * 32
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        comp = File(name="blob.bin", file=tmp)
        data = await plg._fetch_bytes(comp)
        name = comp.name
        assert data == payload and name == "blob.bin"
        container = encoder.pack_file(name, "application/octet-stream", data)
        b64f = plg._encode_to_b64(container)

        # 3) 还原：解析 base64 流 → LTDecoder → assemble → 校验
        stream = base64.urlsafe_b64decode(b64f + "=" * (-len(b64f) % 4))
        block_len = plg.block_len
        k = (len(container) + block_len - 1) // block_len
        frame_len = 20 + block_len
        frames = [stream[i * frame_len : (i + 1) * frame_len] for i in range(k)]
        sid = struct.unpack_from("<H", frames[0], 2)[0]
        dec = LTDecoder(k, block_len, sid, len(container))
        for i, fr in enumerate(frames):
            dec.add_frame(i, fr[20:])
        assert dec.is_complete and dec.assemble() == container
        print(f"file round-trip OK, k={k}, session_id={sid}")

        # 4) URL 形态
        url = plg._build_url(b64f)
        assert url.startswith("https://qr.example.com/r#"), url
        print("url:", url[:60] + "...")
    finally:
        os.unlink(tmp)

    # 5) 超限报错（随机数据避免被 gzip 压缩）
    try:
        plg._encode_to_b64(
            encoder.pack_file(
                "big.bin", "application/octet-stream", os.urandom(300_000)
            )
        )
        raise SystemExit("should have raised")
    except ValueError as e:
        print("over-limit raises OK:", str(e)[:40])

    # 6) host 缺失报错
    plg2 = main.DecimenOpticalTransfer(None, config={"mode": "hosted"})
    try:
        plg2._build_url("x")
        raise SystemExit("should have raised")
    except ValueError as e:
        print("missing-host raises OK:", str(e)[:40])

    # 7) file 模式：自包含 HTML 文件回复

    plg3 = main.DecimenOpticalTransfer(None, config={"mode": "file"})
    container = encoder.pack_snippet("file 模式测试")
    summary = plg3._summarize(container, "文本片段（7 字符）", True)
    chain = await plg3._build_file_reply(container, summary, True, "file")
    assert len(chain) == 2, chain
    file_comp, text_comp = chain
    assert isinstance(file_comp, Comp.File), file_comp
    p = pathlib.Path(file_comp.file)
    assert p.is_file(), "自包含 HTML 文件未生成"
    html = p.read_text(encoding="utf-8")
    assert "%STREAM_B64%" not in html, "帧流未注入"
    assert "QRCode" in html and "rasterizeQr" in html, "库/光栅化缺失"
    assert "file 模式测试" in text_comp.text or text_comp.text, text_comp
    print(f"file mode OK: {file_comp.name} ({p.stat().st_size} B, 含库+流)")

    # 8) both 模式：文件 + URL 都回
    plg4 = main.DecimenOpticalTransfer(
        None, config={"mode": "both", "host": "https://qr.example.com"}
    )
    chain4 = await plg4._build_file_reply(container, summary, True, "both")
    assert len(chain4) == 2
    assert "https://qr.example.com/r#" in chain4[1].text
    print("both mode OK: 文件 + URL")

    # 9) 短链模式：URL 极短（依赖 shorten_server 在 127.0.0.1:8123 运行）
    plg5 = main.DecimenOpticalTransfer(
        None, config={"mode": "hosted", "shorten_host": "http://127.0.0.1:8123"}
    )
    b64s = plg5._encode_to_b64(encoder.pack_snippet("短链测试 hello"))
    url5 = await plg5._build_short_url(b64s)
    assert url5.startswith("http://127.0.0.1:8123/r/"), url5
    assert len(url5) < 80, f"短链仍过长: {len(url5)}"
    print(f"shorten URL OK: {url5} ({len(url5)} 字符)")

    # 10) 短链服务不可达 → 明确报错
    plg6 = main.DecimenOpticalTransfer(
        None, config={"mode": "hosted", "shorten_host": "http://127.0.0.1:1"}
    )
    try:
        await plg6._build_short_url("x")
        raise SystemExit("should have raised")
    except ValueError as e:
        print("shorten-unreachable raises OK:", str(e)[:50])

    print("SMOKE ALL OK")


asyncio.run(main_test())
