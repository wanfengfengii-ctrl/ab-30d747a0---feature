"""配平业务 API（Python 标准库 http.server，零第三方依赖）。

路由
----
- GET  /healthz        健康检查
- POST /api/balance    提交草稿并发起配平
- 其他路径/方法          404 / 405
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .balance import MAX_REQUEST_BYTES, ValidationError, solve


class Handler(BaseHTTPRequestHandler):
    server_version = "FogBalanceAPI/1.0"

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":
            self._send_json(200, {"status": "ok", "service": "balance-api"})
        elif self.path.split("?", 1)[0] == "/api/balance":
            self._send_json(405, {"error": "请使用 POST 提交草稿"})
        else:
            self._send_json(404, {"error": "路径不存在"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/balance":
            if self.path.split("?", 1)[0] == "/healthz":
                self._send_json(405, {"error": "健康检查请使用 GET"})
            else:
                self._send_json(404, {"error": "路径不存在"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, {"error": "请求体为空"})
            return
        if length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "请求体过大"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "请求体不是合法的 JSON"})
            return

        try:
            result = solve(payload)
        except ValidationError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            # 意外错误不吞细节到客户端，但保证响应体仍是规范 JSON
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": f"服务内部错误：{exc}"})
            return
        self._send_json(200, result)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        # 与容器日志习惯保持简洁
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"balance-api listening on {host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
