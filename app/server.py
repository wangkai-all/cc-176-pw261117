"""Local HTTP server: JSON API plus static page, standard library only."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import BatchError, Database
from .seed import CUSTOMERS, DEMO_BATCHES, seed_orders
from .timeutil import parse_timestamp

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
MAX_BODY = 2 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "OrderMetrics/1.0"
    db: Database  # set by make_server

    # ------------------------------------------------------------- utilities
    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, **extra) -> None:
        self._json(status, {"error": message, **extra})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    # ----------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._get_api(path, query)
                return
            self._static(path)
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, f"未找到：{exc.args[0]}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            if parsed.path == "/api/preview":
                result = self.db.preview(payload)
                self._json(HTTPStatus.OK, result)
            elif parsed.path == "/api/commit":
                simulate = bool(payload.pop("simulate_failure", False))
                result = self.db.commit(payload, simulate_failure=simulate)
                status = HTTPStatus.OK if result["committed"] else HTTPStatus.CONFLICT
                self._json(status, result)
            elif parsed.path == "/api/reset":
                self.db.seed(CUSTOMERS, seed_orders(), rebuild=True)
                self._json(HTTPStatus.OK, {"ok": True, "state": self.db.state()})
            else:
                self._error(HTTPStatus.NOT_FOUND, f"未知接口：{parsed.path}")
        except BatchError as exc:
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "批次校验失败", errors=exc.errors)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")

    def _get_api(self, path: str, query: dict) -> None:
        as_of = query.get("as_of", [None])[0]
        as_of_ms = parse_timestamp(as_of) if as_of else None
        if path == "/api/state":
            self._json(HTTPStatus.OK, self.db.state(as_of_ms))
            return
        if path == "/api/verify":
            self._json(HTTPStatus.OK, self.db.verify())
            return
        if path == "/api/audit":
            self._json(HTTPStatus.OK, self.db.audit())
            return
        if path == "/api/demo":
            self._json(
                HTTPStatus.OK,
                {
                    "demos": [
                        {
                            "key": key,
                            "title": demo["title"],
                            "note": demo["note"],
                            "operations": demo["payload"]["operations"],
                        }
                        for key, demo in DEMO_BATCHES.items()
                    ],
                    "definition": self.db.state()["definition"],
                },
            )
            return
        match = re.fullmatch(r"/api/customers/([^/]+)", path)
        if match:
            self._json(
                HTTPStatus.OK,
                self.db.customer_detail(match.group(1), as_of_ms),
            )
            return
        match = re.fullmatch(r"/api/batches/([^/]+)", path)
        if match:
            self._json(HTTPStatus.OK, self.db.batch_detail(match.group(1)))
            return
        self._error(HTTPStatus.NOT_FOUND, f"未知接口：{path}")

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents and target != WEB.resolve():
            self._error(HTTPStatus.FORBIDDEN, "禁止访问")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    db = Database(db_path)
    if db.state()["summary"]["order_count"] == 0:
        db.seed(CUSTOMERS, seed_orders())
    handler = type("BoundHandler", (Handler,), {"db": db})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="订单窗口指标本地控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--db",
        default=str(ROOT / "data" / "metrics.db"),
        help="SQLite 文件路径；默认 data/metrics.db",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="启动后不自动打开浏览器"
    )
    args = parser.parse_args()

    server = make_server(args.host, args.port, args.db)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"订单窗口指标控制台运行于 {url}")
    print(f"数据库：{args.db}")
    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
