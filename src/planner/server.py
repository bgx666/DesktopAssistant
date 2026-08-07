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
import sys
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
                "asr": {"enabled": self.session.asr.enabled,
                        "ready": self.session.asr.ready},
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
        elif path.startswith("/tts/"):
            # 合成音频下载（仅限 tts 目录内的 32 位 hex + .mp3，防路径穿越）
            name = path[len("/tts/"):]
            import re as _re
            if not _re.fullmatch(r"[0-9a-f]{32}\.mp3", name):
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            audio_file = self.session.tts.tts_dir / name
            if not audio_file.is_file():
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            body = audio_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/history":
            # 对话历史（渲染聊天区用）：玩家消息 / 小助回复 / 工具卡片，
            # 以及压缩节点（role=memory，早期对话的记忆摘要，显示在对话框顶部）
            msgs = []
            for m in self.session.recent_buffer:
                meta = getattr(m, "metadata", None) or {}
                if "node_id" in meta:
                    content = str(getattr(m, "content", "") or "")
                    if content:
                        msgs.append({"role": "memory", "content": content,
                                     "node_id": meta["node_id"]})
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
                    msgs.append({"role": "user", "content": content,
                                 "id": getattr(m, "id", None) or ""})
                elif role == "ai":
                    if content:
                        msgs.append({"role": "assistant", "content": content})
                    # 历史工具调用卡片（与实时事件一致：heartbeat 不展示）
                    for tc in (getattr(m, "tool_calls", None) or []):
                        name = tc.get("name", "?")
                        if name == "heartbeat":
                            continue
                        msgs.append({"role": "tool_call", "id": tc.get("id", ""),
                                     "name": name, "args": tc.get("args", {})})
                elif role == "tool":
                    msgs.append({"role": "tool_result",
                                 "id": getattr(m, "tool_call_id", ""),
                                 "content": str(getattr(m, "content", "") or "")})
            self._send_json({"ok": True, "messages": msgs})
        else:
            self._send_json({"ok": False, "error": f"unknown endpoint: {path}"}, status=404)

    # ── POST ──────────────────────────────────────────────────

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/chat":
            body = self._read_json_body()
            message = str(body.get("message", "")).strip()
            files = body.get("files") if isinstance(body.get("files"), list) else None
            if not message and not files:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            msg_id = self.session.enqueue_player_message(message, files)
            self._send_json({"ok": True, "msg_id": msg_id})
        elif path == "/undo":
            body = self._read_json_body()
            msg_id = str(body.get("msg_id", "") or "").strip()
            if not msg_id:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            self._send_json(self.session.undo_message(msg_id))
        elif path == "/stop":
            # 停止当前生成（生成在安全点收尾）
            self.session.request_stop()
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
        elif path == "/typing":
            body = self._read_json_body()
            self.session.set_typing(bool(body.get("typing", False)))
            self._send_json({"ok": True})
        elif path == "/nudge":
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
            self.session._receive(f"[{now}] {PLAYER_NAME}戳了戳你，想看看你在忙什么。", trigger=True)
            self.session._spawn_worker("nudge")
            self._send_json({"ok": True})
        elif path == "/asr":
            # 语音输入：body = 16k/16bit/mono wav 二进制 → 识别文本
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 20 * 1024 * 1024:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            text = self.session.asr.recognize(self.rfile.read(length))
            if text is None:
                self._send_json({"ok": False, "error": "asr_unavailable"}, status=502)
                return
            self._send_json({"ok": True, "text": text})
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
    root = logging.getLogger("planner")
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    if sys.stderr is not None:   # pythonw（无控制台）下 stderr 为 None，跳过控制台日志
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
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
    if sys.stdout is not None:   # pythonw（无控制台）下 stdout 为 None
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
