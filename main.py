"""AstrBot 插件：URL 显示的动态二维码光学传输（decimen wire v2）。

把用户发来的文本 / 附件文件编码成 decimen 兼容的"喷泉帧字节流"，经
base64url 包进 URL 回发到聊天。对方用浏览器打开 URL 看到动画二维码，
再用另一台设备上的 decimen 官方接收端相机扫描，即可还原文件 ——
文件内容全程走光信道，不经过任何网络（设计见 DESIGN_SUMMARY.md）。

当前为阶段 1：编码引擎 + 插件壳已完成并通过 golden 向量验证；播放器模板
（templates/player.html）已实现，部署到 {host}/r/ 后 URL 即可扫描。

安全说明：与 decimen 一致，链路不加密，URL 即承载令牌（bearer）——
任何拿到该 URL 的人都能还原文件，请勿泄露链接。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import sys
from pathlib import Path

# AstrBot 以 `data.plugins.<name>` 模块名加载本文件，插件目录不在 sys.path 中，
# 必须显式注入，否则 `import encoder` / `import fountain_port` 会失败。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_player  # noqa: E402
import encoder  # noqa: E402
import httpx  # noqa: E402

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

_HTTP_PREFIXES = ("http://", "https://")
_BASE64_PREFIX = "base64://"
_FILE_PREFIX = "file://"

_QQ_NOISE_FILTER_INSTALLED = False


def _install_qq_noise_filter() -> None:
    """静默 AstrBot 内核里关于 QQ 图床下载失败的日志。

    背景：gchat.qpic.cn 的下载 URL 依赖 NapCat Rkey 签名服务；该社区服务失效时，
    URL 全部返回 400 "download url has expired"。本插件已通过 NTQQ 本地缓存
    （Pic/<YYYY-MM>/Ori/<md5>.jpg）完全绕开下载，但 AstrBot 内核的图片预处理
    （preprocess_stage → utils.io）仍会尝试下载这些过期 URL 并打印两条报错日志。
    这些报错与插件功能无关（缓存方案已兜底），此处用 logging 过滤器静默之——
    仅匹配 QQ 图床下载失败的特征消息，不影响其他任何日志。

    只修改本插件文件；不改动 AstrBot 内核代码。
    """

    global _QQ_NOISE_FILTER_INSTALLED
    if _QQ_NOISE_FILTER_INSTALLED:
        return
    _QQ_NOISE_FILTER_INSTALLED = True

    class _QpicNoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001 - 防御性：消息格式化异常不拦截
                return True
            if "gchat.qpic.cn" in msg and (
                "Failed to download file" in msg or "Image processing failed" in msg
            ):
                return False  # 静默这条已知无害的报错
            return True

    # AstrBot 内核的 utils.io 与 preprocess_stage 共用 logger "astrbot"。
    _filter = _QpicNoiseFilter()
    for name in ("astrbot", "astrbot.core.utils.io", "astrbot.core.pipeline.preprocess_stage.stage"):
        lg = logging.getLogger(name)
        if _filter not in lg.filters:
            lg.addFilter(_filter)


_install_qq_noise_filter()


class DecimenOpticalTransfer(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default):
        if self.config is None:
            return default
        try:
            return self.config.get(key, default)
        except AttributeError:
            return default

    @property
    def block_len(self) -> int:
        return int(self._cfg("block_len", encoder.DEFAULT_BLOCK_LEN))

    @property
    def use_repair(self) -> bool:
        return bool(self._cfg("use_repair", False))

    @property
    def mode(self) -> str:
        return str(self._cfg("mode", "hosted")).lower()

    @property
    def host(self) -> str:
        return str(self._cfg("host", "")).rstrip("/")

    @property
    def shorten_host(self) -> str:
        """短链服务地址（可选）。设置后 URL 输出为 {shorten_host}/r/<短ID>，
        数据 POST 存到服务端，不再把整段帧流塞进 URL（URL 极短）。"""
        return str(self._cfg("shorten_host", "")).rstrip("/")

    @property
    def max_payload_bytes(self) -> int:
        return int(self._cfg("max_payload_bytes", 200_000))

    # ------------------------------------------------------------------ 指令
    @filter.command("qr")
    async def qr(self, event: AstrMessageEvent, text: str = ""):
        """把文本或回复的附件编码为动态二维码光学传输。用法：
        /qr <文本>         —— 编码一段文本
        回复文件并 @机器人 qr —— 编码一个附件文件
        输出形式由配置 mode 决定：file（自包含 HTML 文件）/ hosted（URL）/ both"""
        try:
            # 1) 文本优先；2) 否则找消息中的附件
            if text.strip():
                container = encoder.pack_snippet(text)
                display = f"文本片段（{len(text)} 字符）"
                is_snippet = True
            else:
                data, name = await self._extract_attachment(event)
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                container = encoder.pack_file(name, mime, data)
                display = f"{name}（{len(data)} 字节，{mime}）"
                is_snippet = False

            mode = self.mode
            summary = self._summarize(container, display, is_snippet)

            if mode in ("file", "both"):
                yield event.chain_result(
                    await self._build_file_reply(container, summary, is_snippet, mode)
                )
                return

            # hosted（URL 方式）：配置了 shorten_host 用短链，否则用原始 hash URL
            b64 = self._encode_to_b64(container, limit=True)
            if self.shorten_host:
                url = await self._build_short_url(b64)
            else:
                url = self._build_url(b64)
            yield event.plain_result(
                f"{summary}\n\n🔗 {url}\n\n"
                "打开方式：对方设备①打开此链接看动画二维码 → 设备②用 decimen 接收端"
                "（decimen.app/receive）相机扫描还原文件。\n"
                "安全提示：此链接即文件本身（不加密），请勿泄露。"
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"qr encode failed: {e}", exc_info=True)
            yield event.plain_result(f"❌ 编码失败：{e}")

    # ------------------------------------------------------------------ 内部
    async def _build_file_reply(self, container: bytes, summary: str, is_snippet: bool, mode: str):
        """自包含 HTML 文件回复（模式 file / both）：内联 QR 库 + 帧流。"""
        b64 = self._encode_to_b64(container, limit=False)  # 文件载体不受 URL 长度限制
        html = build_player.build("file", b64)
        path, file_name = self._save_html(html, summary, is_snippet)

        text = (
            f"{summary}\n\n📄 已生成自包含播放器文件：{file_name}\n"
            "对方操作：下载后用【外部浏览器】打开（微信内置浏览器摄像头支持不稳）→ "
            "看到动画二维码 → 用另一台设备的 decimen 接收端（decimen.app/receive）"
            "相机扫描还原文件。文件走光不走网，零网络。\n"
            "安全提示：此文件即原内容（不加密），请勿泄露。"
        )
        if mode == "both":
            try:
                if self.shorten_host:
                    url = await self._build_short_url(
                        self._encode_to_b64(container, limit=True)
                    )
                else:
                    url = self._build_url(self._encode_to_b64(container, limit=True))
                text += f"\n\n（也可用链接方式打开：{url}）"
            except ValueError as e:
                logger.warning(f"both 模式 URL 生成失败（仅发文件）：{e}")
        # 注意：AstrBot v4.27.2 的 Comp.File 签名为 File(name, file) —— 显示名在前、路径在后。
        return [Comp.File(file_name, path), Comp.Plain(text)]

    def _save_html(self, html: str, summary: str, is_snippet: bool) -> tuple[str, str]:
        """把自包含 HTML 写入插件数据目录，返回 (绝对路径, 文件名)。"""
        data_dir = StarTools.get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        base = "snippet" if is_snippet else encoder.safe_file_name(summary.split("（")[0])
        safe = re.sub(r"[^\w\-.]", "_", base) or "decimen"
        file_name = f"{safe}.html"
        path = data_dir / file_name
        path.write_text(html, encoding="utf-8")
        return str(path), file_name

    def _encode_to_b64(self, container: bytes, limit: bool = True) -> str:
        if limit and len(container) > self.max_payload_bytes:
            raise ValueError(
                f"载荷 {len(container)} 字节超过 URL 模式上限 "
                f"{self.max_payload_bytes} 字节。请减小文件、调大 max_payload_bytes，"
                "或改用 mode=file（自包含 HTML 文件，体积上限宽松）。"
            )
        stream = encoder.encode_stream(
            container,
            block_len=self.block_len,
            use_repair=self.use_repair,
        )
        return encoder.stream_to_base64url(stream)

    def _build_url(self, b64: str) -> str:
        if self.mode == "data":
            # 阶段 1：data:text/html;base64,<播放器 HTML+字节流>
            raise ValueError(
                "data: 模式需播放器页面支持（阶段 1），当前请使用 hosted 模式。"
            )
        if not self.host:
            raise ValueError("请先在插件配置中设置 HOST（托管播放器地址）。")
        return f"{self.host}/r#{b64}"

    async def _build_short_url(self, b64: str) -> str:
        """短链模式：把帧流 POST 到 shorten_host 存下，返回 {host}/r/<短ID>。

        URL 不再携带字节流，极短（如 http://127.0.0.1:8123/r/Ab3xY9）。
        需短链服务（shorten_server.py）已在 shorten_host 上运行。
        """
        shorten = self.shorten_host
        if not shorten:
            raise ValueError("请先在插件配置中设置 SHORTEN_HOST（短链服务地址）。")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(f"{shorten}/api/shorten", json={"b64": b64})
        except httpx.HTTPError as e:
            raise ValueError(
                f"短链服务不可达（{shorten}）：{e}。请确认 shorten_server.py 已启动。"
            ) from e
        if resp.status_code != 200:
            raise ValueError(
                f"短链服务返回 {resp.status_code}：{resp.text[:120]}。"
                "请确认 shorten_host 指向已启动的 shorten_server.py。"
            )
        data = resp.json()
        sid = data.get("id")
        if not sid:
            raise ValueError("短链服务未返回 id。")
        return f"{shorten}/r/{sid}"

    def _summarize(self, container: bytes, display: str, is_snippet: bool) -> str:
        k = (len(container) + self.block_len - 1) // self.block_len
        frames = k * (2 if self.use_repair else 1)
        stream_len = frames * (encoder.HEADER_LEN + self.block_len)
        b64_len = (stream_len + 2) // 3 * 4
        return (
            f"📦 已编码：{display}\n"
            f"└ 容器 {len(container)}B → {k} 块 × {self.block_len}B"
            f"{' +repair' if self.use_repair else ''} = {frames} 帧\n"
            f"└ 流 {stream_len}B → URL 负载约 {b64_len} 字符"
        )

    async def _extract_attachment(self, event: AstrMessageEvent) -> tuple[bytes, str]:
        """从消息链中提取第一个文件/图片附件（字节 + 文件名）。"""
        chain = event.message if hasattr(event, "message") else event.get_messages()
        for comp in chain:
            if isinstance(comp, Comp.File):
                return await self._fetch_bytes(comp, event), (comp.name or "transfer.bin")
            if isinstance(comp, Comp.Image):
                name = getattr(comp, "name", "") or "image.png"
                return await self._fetch_bytes(comp, event), name
        raise ValueError("未找到文本或附件。用法：/qr <文本>，或回复文件并 @机器人 qr")

    async def _fetch_bytes(self, comp, event: AstrMessageEvent | None = None) -> bytes:
        # ① QQ 平台（aiocqhttp）优先：OneBot get_image 拿本地缓存，绕开
        #    gchat.qpic.cn 过期下载 URL（HTTP 400 "download url has expired"）。
        #    注意：AiocqhttpMessageEvent 的 bot（CQHttp 实例）挂在事件对象本身，
        #    不在 message_obj 上（那只是消息内容）。两种位置都兼容探测。
        bot = getattr(event, "bot", None) or getattr(getattr(event, "message_obj", None), "bot", None)
        if bot is not None and isinstance(comp, Comp.Image):
            file_id = getattr(comp, "file", "") or ""
            if file_id and not file_id.startswith(("http://", "https://", "base64://", "file://")):
                # ①-a 先查 QQ 本地图片缓存（NTQQ 数据目录 Pic/YYYY-MM/Ori/<md5>.jpg），
                #     完全不依赖 Rkey 签名服务（gchat.qpic.cn URL 需要 Rkey，服务挂时
                #     URL 全部 400 "download url has expired"）。
                md5 = re.sub(r"[^0-9a-fA-F]", "", file_id)
                if len(md5) == 32:
                    for ts_dir in Path.home().glob("Documents/Tencent Files/*/nt_qq/nt_data/Pic/*/Ori"):
                        probe = ts_dir / f"{md5.lower()}.jpg"
                        if probe.is_file():
                            return probe.read_bytes()
                        probe = ts_dir / f"{md5.lower()}.png"
                        if probe.is_file():
                            return probe.read_bytes()
                fresh_url = ""
                try:
                    ret = await bot.call_action("get_image", file=file_id)
                    ret = ret or {}
                    fresh_url = ret.get("url", "") or ""
                    local = ret.get("file", "") or ""
                    # NapCat 返回的 file 可能是绝对路径，也可能是相对其数据目录的
                    # 路径（如 napcat/xxx.jpg）。跨目录时绝对路径直接读，相对路径
                    # 依次尝试常见 NapCat 数据目录拼接。
                    candidates: list[str] = []
                    if local:
                        candidates.append(local)
                        if local.startswith("file://"):
                            candidates.append(local[len("file://") :])
                    for cand in candidates:
                        p = Path(cand)
                        if p.is_absolute() and p.is_file():
                            return p.read_bytes()
                    if local and not local.startswith(("http://", "https://")):
                        for napcat_root in (
                            Path.home() / "NapCat.OpenClaw",
                            Path.home() / "napcat",
                            Path.home() / ".config" / "napcat",
                        ):
                            for rel in (local, local.lstrip("/\\")):
                                probe = napcat_root / "data" / rel
                                if probe.is_file():
                                    return probe.read_bytes()
                    logger.warning(f"get_image 未返回可读本地路径，尝试新鲜 URL: {local!r}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"OneBot get_image 失败，回退 URL 下载: {e}")
                if fresh_url and fresh_url.startswith(_HTTP_PREFIXES):
                    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                        headers = (
                            {
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/126.0.0.0 Safari/537.36"
                                ),
                                "Referer": "https://qpic.cn/",
                            }
                            if "qpic.cn" in fresh_url
                            else None
                        )
                        resp = await client.get(fresh_url, headers=headers)
                        resp.raise_for_status()
                        return resp.content

        # ② 通用路径：URL / base64 / 本地文件
        url = getattr(comp, "url", "") or ""
        file = getattr(comp, "file", "") or ""
        for field in (url, file):
            if field.startswith(_HTTP_PREFIXES):
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    # QQ 图床（gchat.qpic.cn / qpic.cn）可能要求浏览器 UA，其余 URL 保持原样。
                    headers = (
                        {
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0.0.0 Safari/537.36"
                            ),
                            "Referer": "https://qpic.cn/",
                        }
                        if "qpic.cn" in field
                        else None
                    )
                    resp = await client.get(field, headers=headers)
                    resp.raise_for_status()
                    return resp.content
            if field.startswith(_BASE64_PREFIX):
                return base64.b64decode(field[len(_BASE64_PREFIX) :])
            if field.startswith(_FILE_PREFIX):
                return Path(field[len(_FILE_PREFIX) :]).read_bytes()
            if field:
                p = Path(field)
                if p.is_file():
                    return p.read_bytes()
        raise ValueError("无法解析附件来源（既无 URL 也无本地文件路径）。")

    async def terminate(self):
        """插件卸载/停用时的清理入口。"""
        logger.info("Decimen Optical Transfer 插件已卸载。")
