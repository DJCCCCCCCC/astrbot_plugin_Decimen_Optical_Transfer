"""build_player.py — 播放器模板构建工具（设计文档 §6/§10）。

把 templates/player.html 构建为三种载体之一：
  ① 托管模式（hosted）  ：仅内联 QR 库，保留 %STREAM_B64% 占位符；
                         部署为 {host}/r/index.html，打开 {host}/r#<b64> 时
                         播放器从 location.hash 读流（字节流不进服务器）。
  ② 自包含 HTML 文件    ：内联 QR 库 + 帧流 → 单个 .html 文件（零网络打开，
                         推荐；绕开 URL 长度天花板）。
  ③ data: URL           ：把自包含 HTML 再 base64 进 data:text/html;base64,
                         适合极小载荷（受 URL 长度限制）。

用法：
  python build_player.py --mode hosted --out dist/r/index.html
  python build_player.py --mode file  --stream <b64> --out dist/player.html
  python build_player.py --mode data  --stream <b64>          # 打印 data: URL
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "player.html"
VENDOR_LIB = Path(__file__).resolve().parent / "templates" / "vendor" / "qrcode.min.js"

LIB_PLACEHOLDER = "/*%QRCODE_LIB%*/"
STREAM_PLACEHOLDER = "%STREAM_B64%"


def inline_library(html: str) -> str:
    """把 /*%QRCODE_LIB%*/ 占位符替换为 node-qrcode UMD 内容。

    库内容里的 `</script` 序列会提前闭合 <script> 标签，须转义为
    `<\\/script`（JS 字符串中 `\\/` 等价 `/`，不影响语义）。
    """
    lib = VENDOR_LIB.read_text(encoding="utf-8")
    lib_escaped = lib.replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT")
    if LIB_PLACEHOLDER not in html:
        raise RuntimeError(f"模板缺少 {LIB_PLACEHOLDER} 占位符，无法内联 QR 库。")
    return html.replace(LIB_PLACEHOLDER, lib_escaped)


def inject_stream(html: str, stream_b64: str | None) -> str:
    """把 %STREAM_B64% 替换为帧流 b64；None 时保留占位符（托管模式）。"""
    if stream_b64 is None:
        return html
    if STREAM_PLACEHOLDER not in html:
        raise RuntimeError(f"模板缺少 {STREAM_PLACEHOLDER} 占位符。")
    return html.replace(STREAM_PLACEHOLDER, stream_b64)


def build(
    mode: str,
    stream_b64: str | None = None,
    out: str | None = None,
    template: str | None = None,
    vendor: str | None = None,
) -> str:
    global TEMPLATE, VENDOR_LIB
    if template:
        TEMPLATE = Path(template)
    if vendor:
        VENDOR_LIB = Path(vendor)

    html = TEMPLATE.read_text(encoding="utf-8")
    html = inline_library(html)
    html = inject_stream(html, stream_b64)

    if mode == "hosted":
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(html, encoding="utf-8")
            return f"written: {out} ({len(html.encode('utf-8'))} bytes)"
        return html  # 保留 %STREAM_B64%（运行时读 hash）
    if mode == "file":
        if stream_b64 is None:
            raise ValueError("file 模式必须提供 --stream（或 --stream-file）。")
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(html, encoding="utf-8")
            return f"written: {out} ({len(html.encode('utf-8'))} bytes)"
        return html
    if mode == "data":
        if stream_b64 is None:
            raise ValueError("data 模式必须提供 --stream（或 --stream-file）。")
        data_url = "data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode("ascii")
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(data_url, encoding="utf-8")
            return f"written: {out} (data URL, {len(data_url)} chars)"
        return data_url
    raise ValueError(f"未知模式：{mode}（hosted / file / data）")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 decimen 播放器模板")
    parser.add_argument("--mode", choices=["hosted", "file", "data"], required=True)
    parser.add_argument("--stream", help="base64url 帧流（或留空走 hash/占位）")
    parser.add_argument("--stream-file", help="从文件读取帧流 b64（避免命令行超长）")
    parser.add_argument("--out", help="输出路径；data 模式不写文件时打印 URL")
    parser.add_argument("--template", help="覆盖模板路径（默认 templates/player.html）")
    parser.add_argument("--vendor", help="覆盖 QR 库路径（默认 templates/vendor/qrcode.min.js）")
    args = parser.parse_args()

    stream = args.stream
    if args.stream_file:
        stream = Path(args.stream_file).read_text(encoding="utf-8").strip()

    result = build(args.mode, stream, args.out, args.template, args.vendor)
    if args.mode == "data" and not args.out:
        print(result)
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
