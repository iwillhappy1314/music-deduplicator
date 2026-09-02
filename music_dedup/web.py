"""音乐去重工具的轻量 Web 控制台和后台任务管理。"""

from __future__ import annotations

import json
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any


@dataclass(frozen=True)
class WebConfig:
    """保存 Web 服务监听、任务路径和访问控制配置。"""

    root: str
    quarantine: str
    report: str
    artist_map: str | None
    host: str
    port: int
    token: str
    log_path: str
    run_on_start: bool
    request_delay: float
    lyrics_api_url: str
    artwork_api_url: str


class JobManager:
    """管理 Web 控制台触发的单一后台任务，阻止并发操作音乐库。"""

    _allowed_actions = frozenset({"dedup", "lyrics", "artwork", "all"})

    def __init__(self, config: WebConfig) -> None:
        """初始化任务状态和线程锁。"""

        self.config = config
        self._state_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "action": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
        }

    def start(self, action: str) -> tuple[bool, dict[str, Any]]:
        """启动一个固定类型的后台任务，已有任务运行时拒绝新任务。"""

        if action not in self._allowed_actions:
            return False, {"error": "不支持的任务类型"}
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False, self.status()
            started_at = _now()
            self._state = {
                "status": "running",
                "action": action,
                "started_at": started_at,
                "finished_at": None,
                "exit_code": None,
            }
            thread = threading.Thread(
                target=self._run,
                args=(action,),
                name=f"music-deduplicator-{action}",
                daemon=True,
            )
            self._thread = thread
            _clear_log(Path(self.config.log_path))
            thread.start()
            return True, self.status()

    def wait(self, timeout: float | None = None) -> None:
        """等待当前任务结束，主要用于测试和受控关闭。"""

        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self) -> dict[str, Any]:
        """返回任务状态、最近日志和最新报告摘要。"""

        with self._state_lock:
            state = dict(self._state)
        state["log"] = _read_tail(Path(self.config.log_path))
        state["report_summary"] = _read_report_summary(Path(self.config.report))
        return state

    def _run(self, action: str) -> None:
        """在后台调用命令行任务并把标准输出写入日志。"""

        try:
            from .cli import main as cli_main

            arguments = self._arguments_for(action)
            with Path(self.config.log_path).open("a", encoding="utf-8") as log_file:
                with redirect_stdout(log_file), redirect_stderr(log_file):
                    exit_code = cli_main(arguments)
            status = "success" if exit_code == 0 else "error"
            with self._state_lock:
                self._state.update(
                    status=status,
                    finished_at=_now(),
                    exit_code=exit_code,
                )
        except Exception as error:  # The Web API must expose job failures, not crash the server.
            with Path(self.config.log_path).open("a", encoding="utf-8") as log_file:
                traceback.print_exc(file=log_file)
            with self._state_lock:
                self._state.update(
                    status="error",
                    finished_at=_now(),
                    exit_code=1,
                    error=str(error),
                )

    def _arguments_for(self, action: str) -> list[str]:
        """把 Web 操作映射为固定的安全命令行参数。"""

        arguments = [
            "--root",
            self.config.root,
            "--quarantine",
            self.config.quarantine,
            "--report",
            self.config.report,
            "--apply",
            "--request-delay",
            str(self.config.request_delay),
            "--lyrics-api-url",
            self.config.lyrics_api_url,
            "--artwork-api-url",
            self.config.artwork_api_url,
            "--verbose",
        ]
        if self.config.artist_map:
            arguments.extend(["--artist-map", self.config.artist_map])
        if action == "lyrics":
            arguments.extend(["--skip-dedup", "--fetch-lyrics"])
        elif action == "artwork":
            arguments.extend(["--skip-dedup", "--fetch-artwork"])
        elif action == "all":
            arguments.extend(["--fetch-lyrics", "--fetch-artwork"])
        return arguments


def _now() -> str:
    """返回 UTC ISO 8601 时间字符串。"""

    return datetime.now(timezone.utc).isoformat()


def _clear_log(log_path: Path) -> None:
    """创建日志目录并清空本次任务日志。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")


def _read_tail(log_path: Path, limit: int = 12000) -> str:
    """读取日志末尾有限字符，避免响应过大。"""

    try:
        return log_path.read_text(encoding="utf-8")[-limit:]
    except (OSError, UnicodeError):
        return ""


def _read_report_summary(report_path: Path) -> dict[str, Any]:
    """读取最新报告的摘要和补齐统计，报告不存在时返回空对象。"""

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("summary", {})
    enrichment = payload.get("enrichment", {})
    return {
        "generated_at_utc": payload.get("generated_at_utc"),
        "summary": summary if isinstance(summary, dict) else {},
        "enrichment": enrichment if isinstance(enrichment, dict) else {},
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    """向 HTTP 客户端返回 JSON 响应。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _make_handler(manager: JobManager) -> type[BaseHTTPRequestHandler]:
    """创建绑定指定任务管理器的 HTTP 请求处理器。"""

    class ControlHandler(BaseHTTPRequestHandler):
        """处理控制台页面、状态查询和固定任务启动请求。"""

        def do_GET(self) -> None:
            """处理页面、静态资源和任务状态请求。"""

            if self.path == "/":
                self._send_file("templates/index.html", "text/html; charset=utf-8")
                return
            if self.path == "/assets/app.css":
                self._send_file("static/app.css", "text/css; charset=utf-8")
                return
            if self.path == "/assets/app.js":
                self._send_file("static/app.js", "text/javascript; charset=utf-8")
                return
            if self.path == "/api/status":
                if not self._authorized():
                    _json_response(self, 401, {"error": "访问令牌无效"})
                    return
                _json_response(self, 200, manager.status())
                return
            _json_response(self, 404, {"error": "Not Found"})

        def do_POST(self) -> None:
            """处理固定任务启动请求，不接受任意 shell 命令。"""

            if not self._authorized():
                _json_response(self, 401, {"error": "访问令牌无效"})
                return
            prefix = "/api/run/"
            if not self.path.startswith(prefix):
                _json_response(self, 404, {"error": "Not Found"})
                return
            action = self.path[len(prefix) :]
            accepted, payload = manager.start(action)
            _json_response(self, 202 if accepted else 409, payload)

        def _authorized(self) -> bool:
            """检查可选的 Web 控制台访问令牌。"""

            return not manager.config.token or self.headers.get("X-Auth-Token", "") == manager.config.token

        def _send_file(self, relative_path: str, content_type: str) -> None:
            """从打包的音乐去重模块目录发送一个静态文件。"""

            path = Path(__file__).parent / relative_path
            try:
                body = path.read_bytes()
            except OSError:
                _json_response(self, 404, {"error": "资源不存在"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            """抑制默认访问日志，避免污染任务日志。"""

    return ControlHandler


class ControlHTTPServer(ThreadingHTTPServer):
    """提供无需反向 DNS 查询的控制台 HTTP 服务端。"""

    allow_reuse_address = True

    def server_bind(self) -> None:
        """绑定监听地址并避免 HTTPServer 默认的反向 DNS 查询。"""

        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def run_web_server(config: WebConfig) -> None:
    """启动 Web 控制台，并按配置选择是否启动时自动执行一次去重。"""

    manager = JobManager(config)
    server = ControlHTTPServer((config.host, config.port), _make_handler(manager))
    if config.run_on_start:
        manager.start("dedup")
    print(f"音乐库控制台已启动: http://{config.host}:{config.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
