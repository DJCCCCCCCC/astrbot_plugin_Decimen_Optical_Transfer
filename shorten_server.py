"""shorten_server.py — decimen 短链数据服务。

把 /qr 生成的帧流 b64 存到本地磁盘，返回 6 位短 ID；对方打开
{host}/r/<id> 时，服务端读取数据并用 build_player 动态渲染自包含
播放器 HTML（内联 QR 库 + 帧流）——播放器模板零改动。

用法：
  python shorten_server.py [--port 8123] [--host 0.0.0.0]

插件配置（_conf_schema.json）：
  "shorten_host": "http://127.0.0.1:8123"
  → hosted / both 模式下，URL 输出为 {shorten_host}/r/<6 位短 ID>，
    不再把整段字节流塞进 URL（数据存在本服务，URL 极短）。

安全提示：此服务存储了原始帧流（未加密），等同持有文件本身；
只应在可信环境运行。数据不自动过期，可删除 shorten_data/ 目录清理。
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import build_player

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "shorten_data"

ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ID_LEN = 6
MAX_B64_CHARS = 40 * 1024 * 1024 * 4 // 3  # 40MB 容器 ≈ 54M b64 字符，留足余量

_render_cache: dict[str, str] = {}
_render_lock = threading.Lock()


def _new_id() -> str:
    for _ in range(1000):
        candidate = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))
        if not (DATA_DIR / f"{candidate}.b64").exists():
            return candidate
    raise RuntimeError("短链 ID 空间耗尽（几乎不可能）")


def save_b64(b64: str) -> str:
    """存帧流，返回短 ID。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sid = _new_id()
    (DATA_DIR / f"{sid}.b64").write_text(b64, encoding="ascii")
    return sid


def render_page(sid: str) -> str | None:
    """读取短链数据并渲染自包含播放器 HTML（带缓存）。"""
    path = DATA_DIR / f"{sid}.b64"
    if not path.exists():
        return None
    with _render_lock:
        cached = _render_cache.get(sid)
        if cached:
            return cached
    b64 = path.read_text(encoding="ascii").strip()
    html = build_player.build("file", b64)
    with _render_lock:
        _render_cache[sid] = html
    return html


class Handler(BaseHTTPRequestHandler):
    server_version = "DecimenShorten/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - 标准库签名
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 - 标准库命名
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/", "/r"):
            self._send(
                200,
                (
                    "decimen 短链服务运行中。\n\n"
                    "用法：插件配置 shorten_host 指向本服务，/qr 会返回形如 "
                    f"<本机IP>:{PORT}/r/<6 位 ID> 的短链接。\n"
                    "数据目录：{DATA_DIR}\n"
                ).encode(),
            )
            return
        if path.startswith("/r/"):
            sid = path[len("/r/") :]
            if not sid or any(c not in ID_ALPHABET for c in sid):
                self._json(404, {"error": "bad id"})
                return
            html = render_page(sid)
            if html is None:
                self._json(404, {"error": f"id {sid!r} 不存在或已过期"})
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - 标准库命名
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/api/shorten":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_B64_CHARS + 64:
                self._json(400, {"error": "bad content-length"})
                return
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            b64 = str(payload.get("b64", "")).strip()
            # 快速合法性检查（base64url 字符集 + 4 对齐）
            if not b64 or len(b64) > MAX_B64_CHARS:
                self._json(400, {"error": "bad b64"})
                return
            if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in b64):
                self._json(400, {"error": "b64 must be base64url"})
                return
            # 确认可解码（防垃圾数据）
            pad = "=" * (-len(b64) % 4)
            try:
                base64.urlsafe_b64decode(b64 + pad)
            except Exception:
                self._json(400, {"error": "b64 decode failed"})
                return
            sid = save_b64(b64)
            self._json(200, {"id": sid, "url": f"/r/{sid}", "chars": len(b64)})
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": str(e)})


def main() -> int:
    global PORT
    parser = argparse.ArgumentParser(description="decimen 短链数据服务")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    PORT = args.port
    if not PORT or not (0 < PORT < 65536):
        parser.error("port 必须在 1-65535")
    server = ThreadingHTTPServer((args.host, PORT), Handler)
    print(f"decimen 短链服务已启动: http://{args.host}:{PORT}")
    print(f"数据目录: {DATA_DIR}（删除该目录可清空全部短链）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
