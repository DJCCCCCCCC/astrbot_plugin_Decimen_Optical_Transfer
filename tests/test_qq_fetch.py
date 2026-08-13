"""验证 _fetch_bytes 的 QQ get_image 分支（bot 探测两种位置 + 相对路径拼接）。"""
import asyncio
import os
import pathlib
import sys
import tempfile
import types
import unittest.mock as um

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
star = types.ModuleType("astrbot.api.star")
event_m = types.ModuleType("astrbot.api.event")
comp_m = types.ModuleType("astrbot.api.message_components")


class _FS:
    def __init__(self, context):
        self.context = context


star.Star = _FS
star.Context = object
star.StarTools = types.SimpleNamespace(
    get_data_dir=lambda: pathlib.Path(tempfile.mkdtemp())
)
api.logger = um.MagicMock()
event_m.AstrMessageEvent = object
event_m.filter = types.SimpleNamespace(command=lambda n: lambda f: f)


class File:
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

import main  # noqa: E402


async def test():
    plg = main.DecimenOpticalTransfer(None, {"mode": "file"})

    # A: 本地缓存优先（file=真实 QQ 图片 MD5，url 是过期 gchat）——应直接读
    #    NTQQ 本地缓存（Pic/YYYY-MM/Ori/<md5>.jpg），完全绕开 Rkey/URL。
    img_real = Image(
        name="img.jpg",
        url="https://gchat.qpic.cn/download?fileid=EXPIRED&rkey=DEAD",
        file="3F0C17736D28BCF0F38C88C5AF7F08E4.jpg",
    )

    class NeverBot:
        async def call_action(self, action, **kw):
            raise AssertionError("本地缓存命中，不应调用 get_image")

    evA = type("E", (), {"bot": NeverBot(), "message_obj": type("M", (), {})()})()
    dA = await plg._fetch_bytes(img_real, evA)
    assert dA[:2] == b"\xff\xd8", "应为真实 JPEG（FFD8 头）"
    assert len(dA) > 1000, dA[:20]
    print(f"A. 本地缓存优先命中真实 QQ 图片: OK ({len(dA)} B JPEG)")

    # B: 缓存未命中（不存在的 MD5）→ 走 get_image，bot 在事件本体
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.write(b"FAKE-PNG-QQ-CACHE")
    tmp.close()
    img_miss = Image(
        name="img.png",
        url="https://gchat.qpic.cn/download?fileid=EXPIRED",
        file="00000000000000000000000000000000.jpg",
    )

    class FakeBot:
        def __init__(self):
            self.called = False

        async def call_action(self, action, **kw):
            self.called = True
            assert action == "get_image" and kw.get("file") == img_miss.file, (action, kw)
            return {"file": tmp.name, "url": ""}

    botB = FakeBot()
    evB = type("E", (), {"bot": botB, "message_obj": type("M", (), {})()})()
    dB = await plg._fetch_bytes(img_miss, evB)
    assert dB == b"FAKE-PNG-QQ-CACHE", dB
    assert botB.called, "B: get_image 未被调用"
    print("B. 缓存未命中 → get_image（bot在事件本体）: OK")

    # C: bot 挂在 message_obj（兼容探测）
    botC = FakeBot()
    evC = type("E", (), {"bot": None, "message_obj": type("M", (), {"bot": botC})()})()
    dC = await plg._fetch_bytes(img_miss, evC)
    assert dC == b"FAKE-PNG-QQ-CACHE", dC
    print("C. bot在message_obj（兼容探测）: OK")

    os.unlink(tmp.name)
    print("ALL QQ FETCH PATHS OK")


asyncio.run(test())
