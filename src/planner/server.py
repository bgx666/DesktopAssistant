"""HTTP 层 —— ThreadingHTTPServer + 契约端点（参照 yaya backend / xiaob http.py）。

端点：
- GET  /init      下发 char / state / mode
- GET  /state     轻量状态（heartbeat 倒计时、DND、计划摘要）
- GET  /dequeue   一次性 drain 事件流（顶层带状态）
- POST /chat      玩家消息（立即返回，异步生成）
- POST /task      结构化录入任务（不经 LLM）
- GET  /tasks     任务列表
- GET  /plan      日计划（?date=YYYY-MM-DD）
- POST /plan/done 勾选完成日计划（通知 agent 跟进）
- POST /dnd       免打扰开关（{enabled, until_hour?}）
- POST /nudge     手动戳一下：立即触发一次自主生成
- POST /toggle_mock 运行时切换 Mock/真实 LLM
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config as _config
from .session import BACKEND_TAG, CHARACTER_ID, DISPLAY_NAME, PLAYER_NAME, PlannerSession

_logger = logging.getLogger("planner.server")


class _Handler(BaseHTTPRequestHandler):
    """请求处理器。session 通过 server 实例属性注入。"""

    server_version = "PlannerBackend/1"
    # HTTP/1.0：每请求一连接。本地轮询开销可忽略，且避免 Windows 上 keep-alive
    # 连接拆除竞态导致的偶发 WinError 10053。
    protocol_version = "HTTP/1.0"

    @property
    def session(self) -> PlannerSession:
        return self.server.planner_session  # type: ignore[attr-defined]

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):  # 静默默认 stderr 访问日志，走 logging
        _logger.debug("[http] %s - %s", self.address_string(), fmt % args)

    # ── GET ───────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/init":
            self._send_json({
                "ok": True,
                "backend": BACKEND_TAG,
                "mode": self.session.mode,
                "char": {"name": CHARACTER_ID, "display_name": DISPLAY_NAME},
                "state": self.session.state_dict(),
            })
        elif path == "/state":
            self._send_json({"ok": True, "state": self.session.state_dict()})
        elif path == "/dequeue":
            events = self.session.drain_events()
            self._send_json({"ok": True, "events": events, "state": self.session.state_dict()})
        elif path == "/tasks":
            query = parse_qs(parsed.query)
            status = query.get("status", [None])[0]
            self._send_json({"ok": True, "tasks": self.session.db.list_tasks(status)})
        elif path == "/plan":
            query = parse_qs(parsed.query)
            date_ = query.get("date", [None])[0]
            self._send_json({"ok": True, "plan": self.session.db.get_plan(date_=date_)})
        elif path == "/next":
            # 动态待办队列（按紧急度排序）
            self._send_json({"ok": True, "queue": self.session.db.list_pending()})
        elif path == "/history":
            # 对话历史（渲染聊天区用）：过滤系统注入的触发消息与压缩摘要节点
            msgs = []
            for m in self.session.recent_buffer:
                meta = getattr(m, "metadata", None) or {}
                if "node_id" in meta:
                    continue
                role = getattr(m, "type", "")
                content = str(getattr(m, "content", "") or "")
                if not content:
                    continue
                if role == "human":
                    # 内部注入（[当前待办]/[早晨]/[提醒]/（心跳…））
                    if content.startswith("（") or content.startswith("[当前"):
                        continue
                    if content.startswith("["):
                        if "对你说：" in content:
                            content = content.split("对你说：", 1)[1]
                        else:
                            continue
                    msgs.append({"role": "user", "content": content})
                elif role == "ai":
                    msgs.append({"role": "assistant", "content": content})
            self._send_json({"ok": True, "messages": msgs})
        else:
            self._send_json({"ok": False, "error": f"unknown endpoint: {path}"}, status=404)

    # ── POST ──────────────────────────────────────────────────

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/chat":
            body = self._read_json_body()
            message = str(body.get("message", "")).strip()
            if not message:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            self.session.enqueue_player_message(message)
            self._send_json({"ok": True})
        elif path == "/task":
            body = self._read_json_body()
            title = str(body.get("title", "")).strip()
            if not title:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            tid = self.session.db.create_task(
                title,
                str(body.get("description", "") or ""),
                str(body.get("due_date", "") or "") or None,
                str(body.get("priority", "normal") or "normal"),
            )
            self._send_json({"ok": True, "id": tid})
        elif path == "/plan/done":
            body = self._read_json_body()
            plan_id = int(body.get("plan_id", 0) or 0)
            if plan_id <= 0:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            items = self.session.db.get_plan()
            target = next((p for p in items if p["id"] == plan_id), None)
            if target is None:
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            if not self.session.db.set_plan_status(plan_id, "done"):
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
            self.session._receive(
                f"[{now}] {DISPLAY_NAME}注意到{PLAYER_NAME}勾选了计划 #{plan_id}「{target['content']}」为完成。",
                trigger=True)
            threading.Thread(target=self.session._player_worker, daemon=True).start()
            self._send_json({"ok": True})
        elif path == "/dnd":
            body = self._read_json_body()
            self.session.set_dnd(bool(body.get("enabled", True)),
                                 body.get("until_hour"))
            self._send_json({"ok": True, "dnd": self.session.state_dict()["dnd"]})
        elif path == "/nudge":
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
            self.session._receive(f"[{now}] {PLAYER_NAME}戳了戳你，想看看你在忙什么。", trigger=True)
            self.session._spawn_worker("scheduled")
            self._send_json({"ok": True})
        elif path == "/toggle_mock":
            mode = self.session.toggle_mock()
            self._send_json({"ok": True, "mode": mode})
        else:
            self._send_json({"ok": False, "error": f"unknown endpoint: {path}"}, status=404)


def create_server(session: PlannerSession, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """创建（未启动的）HTTP 服务。port=0 时由系统分配端口（测试用）。"""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.planner_session = session  # type: ignore[attr-defined]
    return httpd


def _setup_logging(data_root) -> None:
    log_dir = data_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_dir / "planner.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root = logging.getLogger("planner")
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def main() -> None:
    port = _config.PLANNER_PORT
    session = PlannerSession()
    _setup_logging(session.data_root)
    session.start_heartbeat()
    httpd = create_server(session, port=port)
    mode = "MOCK（脚本化假 LLM）" if session.mock else "真实 LLM"
    _logger.info("planner 后端启动: http://127.0.0.1:%d 模式=%s 数据目录=%s",
                 port, mode, session.data_root)
    print(f"[planner] 小助后端已启动: http://127.0.0.1:{port} （模式: {mode}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.stop_heartbeat()
        httpd.server_close()
        session.close()
        _logger.info("planner 后端已停止")


if __name__ == "__main__":
    main()
