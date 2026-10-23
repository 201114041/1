from __future__ import annotations

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .pipeline import STEP_NAMES, PipelineService

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
TASK_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_-]+)$")
RETRY_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_-]+)/retry$")


class RequestHandler(BaseHTTPRequestHandler):
    service: PipelineService

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send_json(
                {
                    "status": "ok",
                    "recovered_steps": self.service.recovered_steps,
                    "stats": self.service.storage.stats(),
                }
            )
            return
        if path == "/api/tasks":
            self._send_json({"tasks": self.service.storage.list_tasks()})
            return

        match = TASK_RE.match(path)
        if match:
            task = self.service.storage.get_task(match.group(1))
            if task is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
            else:
                self._send_json(task)
            return

        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if path == "/api/tasks":
            title = str(payload.get("title", "")).strip()[:80] or "动画素材处理任务"
            try:
                step_delay_ms = max(0, min(int(payload.get("step_delay_ms", 500)), 30000))
                fail_times = max(0, min(int(payload.get("fail_times", 0)), 3))
            except (TypeError, ValueError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "耗时或失败次数参数不合法")
                return
            fail_step = payload.get("fail_step") or None
            if fail_step not in STEP_NAMES:
                fail_step = None
            task_input = {
                "asset_name": str(payload.get("asset_name", "demo_anime_pv.mp4"))[:120],
                "step_delay_ms": step_delay_ms,
                "fail_step": fail_step,
                "fail_times": fail_times,
                "pipeline_version": 1,
            }
            task_id = self.service.create_task(title, task_input)
            self._send_json({"id": task_id}, status=HTTPStatus.CREATED)
            return

        match = RETRY_RE.match(path)
        if match:
            task_id = match.group(1)
            if self.service.storage.get_task(task_id) is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
            elif not self.service.storage.retry_task(task_id):
                self._send_error_json(HTTPStatus.CONFLICT, "当前任务没有可重试的失败步骤")
            else:
                self._send_json({"id": task_id, "status": "pending"})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求体为空或过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON 顶层必须是对象")
        return value

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.removeprefix("/static/")
        candidate = (STATIC_DIR / relative).resolve()
        if candidate != STATIC_DIR and STATIC_DIR not in candidate.parents:
            self._send_error_json(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        if not candidate.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "文件不存在")
            return

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types.get(candidate.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} - {format % args}")


class PipelineServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(host: str, port: int, data_dir: Path) -> tuple[PipelineServer, PipelineService]:
    service = PipelineService(data_dir=data_dir, retry_delay_seconds=1.0, auto_start=True)
    handler = type("BoundRequestHandler", (RequestHandler,), {"service": service})
    server = PipelineServer((host, port), handler)
    return server, service


def main() -> None:
    parser = argparse.ArgumentParser(description="Observable AI task pipeline MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=ROOT_DIR / "data")
    args = parser.parse_args()

    server, service = build_server(args.host, args.port, args.data_dir)
    print(f"Pipeline MVP running at http://{args.host}:{args.port}", flush=True)
    if service.recovered_steps:
        print(f"Recovered {service.recovered_steps} interrupted step(s).", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping server...", flush=True)
    finally:
        service.stop()
        server.server_close()


if __name__ == "__main__":
    main()
