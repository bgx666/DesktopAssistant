"""HTTP 层 —— ThreadingHTTPServer + 契约端点（参照 yaya backend / xiaob http.py）。

端点：
- GET  /init      下发 char / state / mode
- GET  /state     轻量状态（heartbeat 倒计时、今日议程摘要）
- GET  /dequeue   一次性 drain 事件流（顶层带状态）
- GET  /history   对话历史（聊天区渲染用）
- GET  /calendar  日历按范围查（?from=YYYY-MM-DD&to=YYYY-MM-DD&cancelled=0/1）
- POST /chat      玩家消息（立即返回，异步生成）
- POST /calendar/add|update|cancel|series/cancel  日历的增改/取消（含整个重复系列）
- POST /nudge     手动戳一下：立即触发一次自主生成
- POST /toggle_mock 运行时切换 Mock/真实 LLM
- GET  /workspace 代码工作区状态与授权规则概要
- POST /workspace 设置/清空代码工作区（{path}；空 = 关闭）
- POST /approval  回填审批结果（{id, decision: allow_once|always|deny, scope?}）
- POST /question  回填 ask_user 提问结果（{id, answer?, selected?}）
- GET  /llm/providers 模型服务商预设 + 当前生效配置（模型菜单用）
- GET  /llm/models    某服务商的可用模型（优先拉端点 /models，失败回退预设）
- GET  /llm/stats     模型用量/缓存命中统计（最近 N 轮生成）
- GET  /file          图片展示（show_image 工具；仅工作区/卸载目录内图片）
- POST /preview       HTML 预览寄存（```html 卡片；{html} → {url}）
- GET  /preview/{id}  预览文档（注入收紧版 CSP：内联脚本可跑、网络/表单/嵌套禁用）
- GET  /changes       文件变更历史（代码工作区写操作留底）
- POST /changes/rollback 回退到某条变更之前（{id}）
- POST /changes/clear 清空变更历史（不动文件）
- POST /permissions/delete|clear 「记住并允许」规则管理
- GET  /dir-permissions 目录读授权列表（递归合并）
- POST /dir-permissions/add|remove|clear 目录读授权管理
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as _config
from . import prompt_loader
from .content import content_text
from .session import BACKEND_TAG, CHARACTER_ID, DISPLAY_NAME, PLAYER_NAME, PlannerSession

_logger = logging.getLogger("planner.server")

#: 预览文档的收紧版 CSP：允许内联脚本/样式（互动演示），禁止一切网络请求
#: （connect-src 'none' 断掉 fetch/XHR 调本地后端 API 的路）与表单/嵌套框架
_PREVIEW_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src data: blob: https: http://127.0.0.1:*; media-src data: blob: https:; "
                "font-src data:; connect-src 'none'; form-action 'none'; frame-src 'none'; "
                "object-src 'none'; base-uri 'none'")


def _wrap_preview_html(html: str) -> str:
    """给 ```html 预览文档注入收紧版 CSP meta（尽量插进已有 <head>，否则包一层壳）。"""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{_PREVIEW_CSP}">'
    low = html.lower()
    if "<head>" in low:
        idx = low.index("<head>") + len("<head>")
        return html[:idx] + meta + html[idx:]
    if "<html" in low:
        i = low.index("<html")
        j = html.find(">", i)
        if j != -1:
            return html[:j + 1] + "<head>" + meta + "</head>" + html[j + 1:]
    return ('<!doctype html><html><head><meta charset="utf-8">' + meta
            + "</head><body>" + html + "</body></html>")


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
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端已断开（长轮询中途退出/窗口关闭）：静默忽略，不刷错误栈
            pass

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

    @staticmethod
    def _valid_date(raw: str) -> str | None:
        """YYYY-MM-DD 校验（非法返回 None）。"""
        import re
        from datetime import date as _date
        s = str(raw or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return None
        try:
            _date.fromisoformat(s)
        except ValueError:
            return None
        return s

    @staticmethod
    def _valid_time(raw: str) -> str | None:
        """HH:MM 归一化（接受 9:05；非法返回 None）。"""
        import re
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw or "").strip())
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        return f"{h:02d}:{mi:02d}"

    def _calendar_note(self, what: str) -> None:
        """用户在日历页的操作 → 静默告知小助（**不触发回复**）。

        日历是用户自己安排的，勾一条就换来一句话会很烦；但小助应当知道，
        所以用 trigger=False 只写进 buffer，等下次被调用时自然看到。
        """
        now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
        self.session._receive(
            prompt_loader.render("runtime/calendar_edit", time=now,
                                 player=PLAYER_NAME, what=what),
            trigger=False, injected=True, kind="calendar_edit")

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
        elif path == "/agents":
            self._send_json(self.session.agents_status())
        elif path == "/state":
            self._send_json({"ok": True, "state": self.session.state_dict()})
        elif path == "/settings":
            self._send_json({"ok": True, "settings": self.session.settings})
        elif path == "/workspace":
            # 代码工作区状态（设置页/标题栏徽标/审批 UI 用）
            from .workspace import PermissionStore
            s = self.session.settings
            ws = str(s.get("workspace_root") or "").strip()
            cwd = self.session.effective_cwd()
            self._send_json({"ok": True, "workspace": ws, "enabled": bool(ws),
                             "cwd": str(cwd) if cwd is not None else "",
                             "recent": list(s.get("workspace_recent") or []),
                             "permission_mode": str(s.get("permission_mode") or "ask"),
                             "rules": PermissionStore(self.session.data_root).load()})
        elif path == "/dir-permissions":
            # 目录只读授权（递归；独立于 permissions.json 的「记住并允许」规则）
            s = self.session.settings
            ws = str(s.get("workspace_root") or "").strip()
            self._send_json({
                "ok": True,
                "grants": self.session.get_permission_db().list_effective(),
                "workspace": ws,
            })
        elif path == "/workspace/dirs":
            # 工作目录面板：列某个目录的直接子目录（仅工作区内；path 相对当前目录）
            query = parse_qs(parsed.query)
            raw = (query.get("path") or [""])[0]
            ws = str(self.session.settings.get("workspace_root") or "").strip()
            if not ws:
                self._send_json({"ok": False, "error": "no_workspace"}, status=400)
                return
            from .workspace import is_blocked, is_within, resolve_target
            ws_path = Path(ws).resolve()
            base = self.session.effective_cwd()
            target = resolve_target(base, raw) if str(raw or "").strip() else base
            if target is None or not is_within(ws_path, target) or not target.is_dir():
                self._send_json({"ok": False, "error": "bad_path"}, status=400)
                return
            dirs = []
            try:
                for p in sorted(target.iterdir(), key=lambda x: x.name.lower()):
                    if p.is_dir() and not p.name.startswith(".") and not is_blocked(p):
                        dirs.append(p.name)
            except OSError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "path": str(target), "dirs": dirs[:100]})
        elif path == "/workspace/files":
            # @ 引用补全：当前目录下的文件列表（相对当前目录，≤50；q 子串过滤）
            query = parse_qs(parsed.query)
            q = (query.get("q") or [""])[0].strip().lower()
            base = self.session.effective_cwd()
            if base is None:
                self._send_json({"ok": False, "error": "no_workspace"}, status=400)
                return
            from .workspace import is_blocked
            skip = {"node_modules", "__pycache__", ".venv", "venv", "dist", "build",
                    ".git", ".idea", ".pytest_cache"}
            out: list[str] = []
            scanned = 0
            try:
                for p in base.rglob("*"):
                    if len(out) >= 50 or scanned >= 4000:
                        break
                    if not p.is_file() or is_blocked(p):
                        continue
                    try:
                        rel = p.relative_to(base).as_posix()
                    except ValueError:
                        continue
                    if any(part in skip for part in rel.split("/")[:-1]):
                        continue
                    scanned += 1
                    if q and q not in rel.lower():
                        continue
                    out.append(rel)
            except OSError:
                pass
            self._send_json({"ok": True, "files": out})
        elif path == "/llm/providers":
            # 服务商预设 + 当前生效配置（标题栏模型菜单 / 设置页）；has_key 用于菜单提示
            from .llm import PROVIDER_PRESETS, resolve_llm_config
            cfg = resolve_llm_config(self.session.settings)
            providers_out = []
            for pid, p in PROVIDER_PRESETS.items():
                pc = resolve_llm_config({**self.session.settings, "llm_provider": pid})
                providers_out.append({
                    "id": pid, "label": p["label"], "base_url": p["base_url"],
                    "models": p["models"], "default": p["default"],
                    "has_key": bool(pc["api_key"]),
                })
            self._send_json({
                "ok": True,
                "providers": providers_out,
                "current": {"provider": cfg["provider"], "model": cfg["model"],
                            "base_url": cfg["base_url"], "has_key": bool(cfg["api_key"])},
            })
        elif path == "/llm/models":
            # 某服务商的可用模型：优先拉端点 models（有 key 时），失败回退预设清单
            query = parse_qs(parsed.query)
            pid = (query.get("provider") or [""])[0].strip().lower()
            from .llm import PROVIDER_PRESETS, provider_headers, resolve_llm_config
            preset = PROVIDER_PRESETS.get(pid)
            if not preset:
                self._send_json({"ok": False, "error": "unknown_provider"}, status=400)
                return
            models: list[str] = []
            source = "preset"
            try:
                import httpx
                cfg = resolve_llm_config({**self.session.settings, "llm_provider": pid})
                if cfg["api_key"]:
                    url = preset.get("models_url") or (cfg["base_url"].rstrip("/") + "/models")
                    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
                    headers.update(provider_headers(pid, self.session.llm_session_id()))
                    r = httpx.get(url, headers=headers, timeout=5)
                    r.raise_for_status()
                    data = r.json()
                    ids = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
                    if ids:
                        models = ids[:80]
                        source = "live"
            except Exception:   # 离线 / key 失效 / 端点不支持 → 回退预设
                models = []
            if not models:
                models = list(preset["models"])
            self._send_json({"ok": True, "models": models, "source": source})
        elif path == "/llm/stats":
            # 模型用量/缓存命中统计（最近 N 轮生成）：状态条「📊」面板
            self._send_json({"ok": True,
                             "summary": self.session.llm_stats.summary(),
                             "records": self.session.llm_stats.list()})
        elif path.startswith("/preview/"):
            # HTML 预览文档（POST /preview 寄存后的取用）：包一层收紧版 CSP 返回。
            # 走 http 而不是 srcdoc：srcdoc 会继承面板的 CSP（script-src 'self'）→ 内联脚本被拦。
            pid = path[len("/preview/"):]
            html = (getattr(self.session, "_previews", {}) or {}).get(pid)
            if not html:
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            body = _wrap_preview_html(html).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/file":
            # 图片展示（show_image 工具）：只允许工作区 / 助手卸载目录内的图片，
            # 防路径穿越；格式与大小白名单。
            from urllib.parse import parse_qs as _parse_qs, unquote
            q = _parse_qs(parsed.query)
            raw = unquote((q.get("path") or [""])[0])
            if not raw:
                self._send_json({"ok": False, "error": "bad_path"}, status=400)
                return
            from .workspace import is_blocked, is_within
            p = Path(raw)
            try:
                p = p.resolve()
            except OSError:
                self._send_json({"ok": False, "error": "bad_path"}, status=400)
                return
            allowed_roots = [Path(str(self.session.data_root)) / "tool_results",
                             Path(str(self.session.data_root)) / "images",
                             Path(str(self.session.data_root)) / "attachments"]
            ws_raw = str(self.session.settings.get("workspace_root") or "").strip()
            if ws_raw:
                try:
                    allowed_roots.append(Path(ws_raw).resolve())
                except OSError:
                    pass
            if not any(is_within(root, p) for root in allowed_roots):
                self._send_json({"ok": False, "error": "forbidden"}, status=403)
                return
            if is_blocked(p) or p.suffix.lower() not in {
                    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                self._send_json({"ok": False, "error": "forbidden"}, status=403)
                return
            if not p.is_file():
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            try:
                if p.stat().st_size > 15 * 1024 * 1024:
                    self._send_json({"ok": False, "error": "too_large"}, status=413)
                    return
                body = p.read_bytes()
            except OSError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            content_type = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }[p.suffix.lower()]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/changes":
            # 文件变更历史（新→旧）；?limit= 可选
            query = parse_qs(parsed.query)
            try:
                limit = max(1, min(500, int((query.get("limit") or ["200"])[0])))
            except (TypeError, ValueError):
                limit = 200
            db = self.session.get_changes_db()
            self._send_json({"ok": True, "changes": db.list(limit=limit)})
        elif path == "/dequeue":
            # 长轮询：?wait=N（秒，0~30）→ 无事件时服务端等待至多有事件/超时；
            # 不带 wait 保持立即返回（测试/旧客户端兼容）。前端 main.js 传 ?wait=25。
            query = parse_qs(parsed.query)
            wait = query.get("wait", [None])[0]
            timeout = None
            if wait is not None:
                try:
                    timeout = max(0.0, min(30.0, float(wait)))
                except (TypeError, ValueError):
                    timeout = None
            events = self.session.drain_events(timeout=timeout)
            self._send_json({"ok": True, "events": events, "state": self.session.state_dict()})
        elif path == "/calendar":
            # 日历：按日期范围取日程/待办（闭区间；缺省=今天）
            query = parse_qs(parsed.query)
            today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            frm = (query.get("from") or [today])[0] or today
            to = (query.get("to") or [frm])[0] or frm
            if to < frm:
                frm, to = to, frm
            include_cancelled = (query.get("cancelled") or ["0"])[0] in ("1", "true")
            self._send_json({"ok": True, "from": frm, "to": to,
                             "items": self.session.db.list_calendar_range(
                                 frm, to, include_cancelled=include_cancelled)})
        elif path == "/tts/voices":
            # 可用音色列表（设置界面下拉；?engine=mimo/local/cloud 可预览指定引擎）
            query = parse_qs(parsed.query)
            engine = (query.get("engine") or [None])[0]
            self._send_json({"ok": True, "voices": self.session.tts.list_voices(engine=engine)})
        elif path == "/tts/say":
            # 气泡按钮/测试按钮合成整句（text → wav），返回 /tts/{name} URL
            query = parse_qs(parsed.query)
            text = (query.get("text") or [""])[0]
            voice = (query.get("voice") or [None])[0]
            _logger.info("[tts] /tts/say 请求（%d 字，voice=%s）：%s", len(text), voice or "-", text[:40])
            if not text.strip():
                self._send_json({"ok": False, "error": "empty_text"}, status=400)
                return
            if voice:
                valid_voices = {v["id"] for v in self.session.tts.list_voices()}
                if voice not in valid_voices:
                    self._send_json({"ok": False, "error": "bad_voice"}, status=400)
                    return
            url = self.session.tts.synthesize(text, voice=voice)
            if url:
                self._send_json({"ok": True, "url": url})
            else:
                self._send_json({"ok": False, "error": "tts_unavailable"}, status=500)
        elif path.startswith("/tts/"):
            # 合成音频下载（仅限 tts 目录内的 32 位 hex + .mp3/.wav，防路径穿越）
            name = path[len("/tts/"):]
            import re as _re
            if not _re.fullmatch(r"[0-9a-f]{32}\.(mp3|wav)", name):
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            audio_file = self.session.tts.tts_dir / name
            if not audio_file.is_file():
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            body = audio_file.read_bytes()
            self.send_response(200)
            content_type = ("audio/wav" if name.endswith(".wav") else "audio/mpeg")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/history":
            # 子代理 White 的独立历史（前端 White 标签页用）
            q = parse_qs(parsed.query)
            if (q.get("agent") or [""])[0] == "white":
                self._send_json(self.session.white_history())
                return
            # 对话历史（渲染聊天区用）：玩家消息 / 小助回复 / 工具卡片，
            # 以及压缩节点（role=memory，早期对话的记忆摘要，显示在对话框顶部）
            msgs = []
            turn_change_ids: list[int] = []   # 本轮改动汇总（挂在回复下面的快捷块）
            turn_autonomous = False           # 当前回合是否自主回合（注入消息触发）

            def _flush_changed() -> None:
                """把本轮改动聚合条目挂进消息流（在最终回复之后）。"""
                if not turn_change_ids:
                    return
                files = self.session.changed_files_for_ids(turn_change_ids)
                if files:
                    msgs.append({"role": "changed_files", "files": files})
                turn_change_ids.clear()

            for m in self.session.recent_buffer:
                meta = getattr(m, "metadata", None) or {}
                if "node_id" in meta:
                    content = content_text(getattr(m, "content", "") or "")
                    if content:
                        msgs.append({"role": "memory", "content": content,
                                     "node_id": meta["node_id"]})
                    continue
                role = getattr(m, "type", "")
                content = content_text(getattr(m, "content", "") or "")
                tool_calls = getattr(m, "tool_calls", None) or []
                tool_ref = getattr(m, "tool_call_id", "") or ""
                # ⚠️ 不能"空内容就整条跳过"：模型**直接调工具、不先说话**时正文是空的，
                # 但它的工具卡片必须照样显示——否则展开面板时（前端 onPanelShown 会
                # 清空消息区再按本接口重建）卡片凭空消失，表现为"有时候不显示工具调用"。
                if not content and not tool_calls and not tool_ref:
                    continue
                if role == "human":
                    # 只有**真正的回合边界**才 flush / 更新自主标记：
                    # - 真实用户消息（不带 injected 标记，且有「对你说：」）；
                    # - 系统回合起点（心跳/日历到点/逾期，metadata.kind 显式标注；
                    #   旧 buffer 按「（系统：」前缀兜底）。
                    # 图片注入、戳一戳、日历页编辑等是**回合中途**注入，不能切块。
                    injected = bool(meta.get("injected"))
                    kind = str(meta.get("kind") or "")
                    is_real_user = (not injected) and (
                        "对你说：" in content
                        or (not content.startswith("（")
                            and not content.startswith("[")))
                    is_system_start = (
                        ((injected and kind in ("heartbeat", "calendar_due",
                                                "calendar_overdue"))
                         or content.startswith("（系统：")))
                    if is_real_user or is_system_start:
                        _flush_changed()   # 回合边界：上一段未出块的改动先统一落块
                        turn_autonomous = not is_real_user
                    # 内部注入（[当前待办]/[早晨]/[提醒]/（心跳…））不显示在对话区
                    if (injected or content.startswith("（")
                            or content.startswith("[当前")):
                        continue
                    # v0.1.295 之前的旧消息没有 metadata.attachments：从注入说明里
                    # 解析附件，并把说明从气泡正文剥离（否则用户看到一串路径）。
                    legacy_atts = []
                    if not meta.get("display_text") and "【拖入的文件】" in content:
                        content, legacy_atts = self.session.split_legacy_attachments(content)
                    if content.startswith("["):
                        if "对你说：" in content:
                            content = content.split("对你说：", 1)[1]
                        else:
                            continue
                    # 用户消息展示原文（去掉注入的附件说明），附件单独给前端渲染
                    if meta.get("display_text"):
                        content = str(meta["display_text"])
                    msgs.append({"role": "user", "content": content,
                                 "id": getattr(m, "id", None) or "",
                                 "attachments": meta.get("attachments") or legacy_atts})
                elif role == "ai":
                    # 思考过程（持久化在 additional_kwargs）：独立条目，前端渲染成
                    # 折叠单行的思考行（历史重建与实时 thinking_stream 同一套样式）
                    reasoning = str((getattr(m, "additional_kwargs", None) or {})
                                    .get("reasoning_content") or "")
                    if reasoning.strip():
                        msgs.append({"role": "thinking", "content": reasoning})
                    if content:
                        msgs.append({"role": "assistant", "content": content})
                        if not tool_calls and not turn_autonomous:
                            _flush_changed()   # 玩家回合的最终回复 → 改动块挂它下面
                    # 历史工具调用卡片（与实时事件 _emit_tool_events 保持一致）
                    for tc in tool_calls:
                        name = tc.get("name", "?")
                        if name == "continue_speaking":
                            continue   # 分段工具不展示卡片（视觉上就是连续说话）
                        msgs.append({"role": "tool_call", "id": tc.get("id", ""),
                                     "name": name, "args": tc.get("args", {})})
                    # 玩家回合以 heartbeat 收尾（调度下一条消息）→ 也是回合终点：
                    # 改动块挂在它后面（与实时侧"player 回合结束即出块"一致）
                    if not turn_autonomous and any(
                            tc.get("name") == "heartbeat" for tc in tool_calls):
                        _flush_changed()
                elif role == "tool":
                    if getattr(m, "name", "") == "continue_speaking":
                        continue
                    # 写操作留底关联（写进消息 additional_kwargs 的 change_ids）
                    # → 历史重建也显示「修改前后」diff
                    ak = getattr(m, "additional_kwargs", None) or {}
                    ids = ak.get("change_ids") or []
                    turn_change_ids.extend(int(i) for i in ids)
                    diff = (self.session.change_diff([int(i) for i in ids],
                                                     tool=getattr(m, "name", "") or "")
                            if ids else None)
                    msgs.append({"role": "tool_result",
                                 "id": tool_ref,
                                 "content": str(getattr(m, "content", "") or ""),
                                 "diff": diff})
                    # show_image 展示过的图片（additional_kwargs.image_paths）→ 图片条目
                    from .session import image_url as _image_url
                    captions = ak.get("image_captions") or []
                    for i, img_path in enumerate(ak.get("image_paths") or []):
                        msgs.append({"role": "image",
                                     "url": _image_url(str(img_path)),
                                     "caption": str(captions[i]) if i < len(captions) else ""})
            _flush_changed()   # 兜底：改动发生在最后、回复没带正文时
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
            msg_id = self.session.enqueue_player_message(message, files,
                                                         with_meta=True)
            if isinstance(msg_id, dict):
                self._send_json({"ok": True, **msg_id})
            else:
                self._send_json({"ok": True, "msg_id": msg_id})
        elif path == "/undo":
            body = self._read_json_body()
            msg_id = str(body.get("msg_id", "") or "").strip()
            if not msg_id:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            self._send_json(self.session.undo_message(msg_id))
        elif path == "/stop":
            # 停止当前生成（含立即唤醒 continue 分段暂停），安全位置收尾
            self.session.request_stop()
            self.session.interrupt_continue_pause()
            self._send_json({"ok": True})
        elif path == "/calendar/add":
            body = self._read_json_body()
            date_ = str(body.get("date", "") or "").strip()
            content = str(body.get("content", "") or "").strip()
            time_ = str(body.get("time", "") or "").strip() or None
            detail = str(body.get("detail", "") or "")
            kind = str(body.get("kind", "event") or "event")
            if not self._valid_date(date_) or not content:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            if time_ is not None:
                t = self._valid_time(time_)
                if t is None:
                    self._send_json({"ok": False, "error": "bad_time"}, status=400)
                    return
                time_ = t
            if kind not in ("event", "task"):
                kind = "event"
            repeat_ = str(body.get("repeat", "") or "").strip().lower()
            if repeat_:
                if repeat_ not in ("daily", "weekly", "weekdays"):
                    self._send_json({"ok": False, "error": "bad_repeat"}, status=400)
                    return
                sid, cnt = self.session.db.add_calendar_series(
                    date_, content, repeat_, time_, kind, detail)
                if not sid:
                    self._send_json({"ok": False, "error": "bad_request"}, status=400)
                    return
                self._calendar_note(
                    f"往日历加了重复日程（{repeat_}）{date_} {time_ or '全天'}「{content}」")
                self._send_json({"ok": True, "series_id": sid, "count": cnt})
                return
            iid = self.session.db.add_calendar_item(date_, content, time_, kind, detail)
            self._calendar_note(f"往日历加了 {date_} {time_ or '全天'}「{content}」")
            self._send_json({"ok": True, "id": iid})
        elif path == "/calendar/update":
            body = self._read_json_body()
            iid = int(body.get("id", 0) or 0)
            if iid <= 0 or self.session.db.get_calendar_item(iid) is None:
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            fields: dict = {}
            if "date" in body:
                d = self._valid_date(str(body.get("date", "") or ""))
                if d is None:
                    self._send_json({"ok": False, "error": "bad_date"}, status=400)
                    return
                fields["date"] = d
            if "time" in body:
                raw = str(body.get("time", "") or "").strip()
                if not raw:
                    fields["time"] = None
                else:
                    t = self._valid_time(raw)
                    if t is None:
                        self._send_json({"ok": False, "error": "bad_time"}, status=400)
                        return
                    fields["time"] = t
            for key in ("content", "detail", "kind", "status"):
                if key in body:
                    fields[key] = body.get(key)
            if fields.get("kind") not in (None, "event", "task"):
                unset = {"kind"}
                fields = {k: v for k, v in fields.items() if k not in unset}
            if fields.get("status") not in (None, "todo", "done", "cancelled"):
                fields.pop("status", None)
            if not fields:
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            self.session.db.update_calendar_item(iid, **fields)
            self._calendar_note(f"改了日历 #{iid}（{'、'.join(fields)}）")
            self._send_json({"ok": True, "item": self.session.db.get_calendar_item(iid)})
        elif path == "/calendar/cancel":
            body = self._read_json_body()
            iid = int(body.get("id", 0) or 0)
            item = self.session.db.get_calendar_item(iid)
            if item is None:
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            self.session.db.cancel_calendar_item(iid)
            self._calendar_note(f"取消了日历 #{iid}「{item['content']}」")
            self._send_json({"ok": True})
        elif path == "/calendar/series/cancel":
            # 取消重复系列"从这一天起、今后所有未完成的"（已完成的不动）
            body = self._read_json_body()
            iid = int(body.get("id", 0) or 0)
            item = self.session.db.get_calendar_item(iid)
            if item is None or not item.get("series_id"):
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            n = self.session.db.cancel_calendar_series(item["series_id"],
                                                       from_date=item["date"])
            self._calendar_note(
                f"取消了重复日程「{item['content']}」自 {item['date']} 起共 {n} 期")
            self._send_json({"ok": True, "count": n})
        elif path == "/settings":
            body = self._read_json_body()
            try:
                settings = self.session.update_settings(body.get("updates") or {})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "settings": settings})
        elif path == "/white/stop":
            self._send_json(self.session.white_stop())
        elif path == "/nudge":
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
            self.session._receive(
                f"[{now}] {PLAYER_NAME}戳了戳你，想看看你在忙什么。",
                trigger=True, injected=True, kind="nudge")
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
            body = self._read_json_body()
            mode = self.session.toggle_mock(str(body.get("mode") or ""))
            self._send_json({"ok": True, "mode": mode})
        elif path == "/workspace":
            body = self._read_json_body()
            raw = str(body.get("path", "") or "").strip()
            if raw:
                target = Path(raw).expanduser()
                try:
                    target = target.resolve()
                except OSError:
                    target = None
                if target is None or not target.is_dir():
                    self._send_json({"ok": False, "error": "not_a_directory"}, status=400)
                    return
                raw = str(target)
            try:
                settings = self.session.update_settings({"workspace_root": raw})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            cwd = self.session.effective_cwd()
            self._send_json({"ok": True,
                             "workspace": settings.get("workspace_root", ""),
                             "enabled": bool(settings.get("workspace_root")),
                             "cwd": str(cwd) if cwd is not None else "",
                             "recent": settings.get("workspace_recent", []),
                             "permission_mode": settings.get("permission_mode", "ask")})
        elif path == "/workspace/cwd":
            # 切换当前工作目录（必须工作区内；空串 = 回到工作区根）
            body = self._read_json_body()
            raw = str(body.get("path", "") or "").strip()
            ws = str(self.session.settings.get("workspace_root") or "").strip()
            if not ws:
                self._send_json({"ok": False, "error": "no_workspace"}, status=400)
                return
            from .workspace import is_within
            ws_path = Path(ws).resolve()
            if raw:
                target = Path(raw).expanduser()
                try:
                    target = target.resolve()
                except OSError:
                    target = None
                if (target is None or not target.is_dir()
                        or not is_within(ws_path, target)):
                    self._send_json({"ok": False, "error": "bad_path"}, status=400)
                    return
                raw = str(target)
            try:
                settings = self.session.update_settings({"workspace_cwd": raw})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            cwd = self.session.effective_cwd()
            self._send_json({"ok": True, "cwd": str(cwd) if cwd is not None else "",
                             "workspace": settings.get("workspace_root", ""),
                             "recent": settings.get("workspace_recent", []),
                             "permission_mode": settings.get("permission_mode", "ask")})
        elif path == "/approval":
            body = self._read_json_body()
            req_id = str(body.get("id", "") or "").strip()
            decision = str(body.get("decision", "") or "").strip().lower()
            if not req_id or decision not in ("allow_once", "always", "deny"):
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            ok = self.session.resolve_approval(req_id, decision, body.get("scope"))
            self._send_json({"ok": ok, "error": None if ok else "not_found"},
                            status=200 if ok else 404)
        elif path == "/question":
            body = self._read_json_body()
            req_id = str(body.get("id", "") or "").strip()
            answer = str(body.get("answer", "") or "")
            selected = str(body.get("selected", "") or "")
            if not req_id or (not answer.strip() and not selected.strip()):
                self._send_json({"ok": False, "error": "bad_request"}, status=400)
                return
            ok = self.session.resolve_question(req_id, answer, selected)
            self._send_json({"ok": ok, "error": None if ok else "not_found"},
                            status=200 if ok else 404)
        elif path == "/preview":
            # HTML 预览寄存（```html 代码块 → iframe 卡片）：前端 POST html → 拿 url；
            # 取用时（GET /preview/{id}）包一层收紧版 CSP。内存缓存，环形上限。
            body = self._read_json_body()
            html = str(body.get("html") or "")
            if not html.strip():
                self._send_json({"ok": False, "error": "empty"}, status=400)
                return
            if len(html) > 1_000_000:
                self._send_json({"ok": False, "error": "too_large"}, status=413)
                return
            store = getattr(self.session, "_previews", None)
            if store is None:
                store = {}
                self.session._previews = store
            if len(store) >= 60:
                for k in list(store.keys())[:30]:
                    store.pop(k, None)
            import uuid as _uuid
            pid = _uuid.uuid4().hex[:16]
            store[pid] = html
            self._send_json({"ok": True, "url": "/preview/" + pid})
        elif path == "/changes/rollback":
            # 回退到某条变更之前（把 id ≥ 该值且未回退的变更从新到旧恢复）
            body = self._read_json_body()
            try:
                cid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "bad_id"}, status=400)
                return
            from .changes import rollback_to
            db = self.session.get_changes_db()
            if db.get(cid) is None:
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            ws_raw = str(self.session.settings.get("workspace_root") or "").strip()
            workspace = Path(ws_raw).resolve() if ws_raw else None
            result = rollback_to(db, cid, workspace)
            self.session.push_event({"type": "changes_update"})
            self._send_json({"ok": True, **result})
        elif path == "/changes/clear":
            self.session.get_changes_db().clear()
            self._send_json({"ok": True})
        elif path == "/dir-permissions/add":
            body = self._read_json_body()
            raw = str(body.get("path") or "").strip()
            r = self.session.get_permission_db().add_read(raw, source="user")
            if not r.get("ok"):
                self._send_json(r, status=400)
                return
            self.session.push_event({"type": "permissions_update"})
            self._send_json({"ok": True, **r})
        elif path == "/dir-permissions/remove":
            body = self._read_json_body()
            try:
                gid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "bad_id"}, status=400)
                return
            db = self.session.get_permission_db()
            if not db.remove(gid):
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            self.session.push_event({"type": "permissions_update"})
            self._send_json({"ok": True, "grants": db.list_effective()})
        elif path == "/dir-permissions/clear":
            db = self.session.get_permission_db()
            db.clear()
            self.session.push_event({"type": "permissions_update"})
            self._send_json({"ok": True, "grants": []})
        elif path == "/permissions/delete":
            from .workspace import PermissionStore
            body = self._read_json_body()
            store = PermissionStore(self.session.data_root)
            rules = store.load()
            try:
                idx = int(body.get("index"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "bad_index"}, status=400)
                return
            if not (0 <= idx < len(rules)):
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            removed = rules.pop(idx)
            store.save(rules)
            self._send_json({"ok": True, "removed": removed, "rules": rules})
        elif path == "/permissions/clear":
            from .workspace import PermissionStore
            PermissionStore(self.session.data_root).clear()
            self._send_json({"ok": True, "rules": []})
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

    # 把 LLM SDK 的告警也收进同一个日志文件。
    # 起因：LLM 客户端是 max_retries=3 / timeout=60（llm.py），但重试告警由
    # openai._base_client / httpx 发出，而 handler 只挂在 "planner" 这个 logger 上
    # ——根 logger 没配，于是「失败后重试但最终成功」完全不留痕。
    # 症状就是「某个回答莫名等了几十秒，而日志里一行都没有」，无法归因。
    # 只挂 WARNING 以上：INFO/DEBUG 会把每次请求都刷成噪音（也可能带出请求细节）。
    for name in ("openai", "httpx", "httpcore", "langchain_openai", "langgraph"):
        sdk = logging.getLogger(name)
        sdk.setLevel(logging.WARNING)
        sdk.addHandler(file_handler)
        if sys.stderr is not None:
            sdk.addHandler(stream_handler)
        sdk.propagate = False   # 不再向根 logger 冒泡，避免重复输出


# ── 启动自检：端口被占时清理残留的 planner 后端 ─────────────

def _pid_file(data_root) -> Path:
    """当前数据目录下的后端 PID 记录（下次启动清理残留用）。"""
    return Path(data_root) / "server.pid"


def is_planner_cmdline(cmdline: str | None) -> bool:
    """命令行是否属于 planner 后端（纯函数，便于测试）。

    识别依据：命令行含 "planner"（python -m planner / venv 路径含
    planner 等）。误杀面极小——本机同时跑一个命令行恰好带 planner
    的无关程序的概率可以忽略。
    """
    return bool(cmdline) and "planner" in cmdline.lower()


def should_kill_stale(pid_in_file: int | None, current_pid: int,
                      cmdline: str | None) -> bool:
    """是否应清理 pid 文件记录的残留后端（纯函数，便于测试）。

    条件缺一不可：有记录 / 不是当前进程自己 / 命令行验明是 planner。
    """
    if not pid_in_file or pid_in_file == current_pid:
        return False
    return is_planner_cmdline(cmdline)


def _process_cmdline(pid: int) -> str | None:
    """读 Windows 进程命令行（杀前验明正身，防误杀复用 PID）；失败返回 None。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=5,
        )
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def ensure_port_free(port: int, data_root) -> bool:
    """端口被占时清理残留的 planner 后端进程；非 planner 占用不误杀。

    返回 True = 端口可用（原本空闲或已清理成功）；
    False = 被其他程序占用或无法安全确认，需人工处理。
    """
    import socket as _socket

    def busy() -> bool:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True
        finally:
            s.close()

    for _ in range(3):
        if not busy():
            return True
        pid_in_file: int | None = None
        try:
            pid_in_file = int(_pid_file(data_root).read_text().strip())
        except (OSError, ValueError):
            pid_in_file = None
        cmdline = _process_cmdline(pid_in_file) if pid_in_file else None
        if not should_kill_stale(pid_in_file, os.getpid(), cmdline):
            _logger.warning("[server] 端口 %s 被占用且无法确认是残留后端"
                            "（PID 记录缺失/不匹配），请手动释放", port)
            return False
        try:
            os.kill(pid_in_file, 9)
            _logger.warning("[server] 已清理残留后端进程 %s（释放端口 %s）",
                            pid_in_file, port)
        except OSError as exc:
            _logger.warning("[server] 清理残留进程 %s 失败: %s", pid_in_file, exc)
        time.sleep(1.0)
    return not busy()


def main() -> None:
    port = _config.PLANNER_PORT
    session = PlannerSession()
    _setup_logging(session.data_root)
    if not ensure_port_free(port, session.data_root):
        _logger.error("[server] 端口 %s 被占用且无法安全清理，退出", port)
        if sys.stdout is not None:
            print(f"[planner] 端口 {port} 被其他程序占用且无法安全清理，请手动释放后重试")
        raise SystemExit(1)
    try:   # 记录本进程 PID：下次启动据此识别并清理残留
        _pid_file(session.data_root).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    session.start_heartbeat()
    httpd = create_server(session, port=port)
    # 服务已监听后再预加载 ASR 模型：模型初始化是 CPU/GIL 密集操作，
    # 若在 PlannerSession 构造时启动会拖慢 /init 与首个 /dequeue。
    # 这里仍然满足“首次启动就加载”，只是不再抢在 HTTP 响应之前。
    if not session.mock:
        timer = threading.Timer(0.2, session.asr.start_prepare)
        timer.daemon = True
        timer.start()
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
