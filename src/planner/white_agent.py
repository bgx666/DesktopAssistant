"""White —— 临时、异步、无长期记忆的子代理。

- 由主助手创建/派活/停止/关闭；用户只和主助手对话；
- 后台线程独立运行（不占主助手的 chat_lock），生命周期跨多个任务；
- 没有记忆树；上下文用"摘要 + 滑动窗口"压缩；
- 工具只允许网页 + 工作区文件（**不含 run_command**，文件操作硬限制在工作区）；
- 停止立即关闭本任务独立模型的 HTTP 客户端（不等 API 超时）；正在写的文件先写完，已有上下文保留、未完成的半句不进上下文；
- 结束时通过 `finish_task` 提交结构化结果，运行器自动补充改动文件/用量；
- 完成/失败/停止后给主助手注入一条紧凑通知（主助手验收/决定关不关）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, hook_config
from langchain_core.messages import (HumanMessage, RemoveMessage, messages_to_dict)
from langchain_core.tools import tool
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from . import prompt_loader
from .agent_context import set_current_agent
from .content import content_text
from .middleware import (ApprovalMiddleware, LoggingMiddleware, PlannerState,
                         SummarizationMiddleware)
from .tools import build_tools

_logger = logging.getLogger("planner.white")

WHITE_ID = "white"
WHITE_NAME = "White"

KEEP_RECENT = 16              # 滑动窗口保留的最近消息条数
COMPACT_MAX_MSGS = 40         # 压缩时最多纳入的旧消息条数
COMPACT_MSG_CHARS = 600       # 压缩输入里每条消息的最大字符数
COMPACT_OUT_CHARS = 4000      # 摘要最大字符数


def _close_stream_bg(gen) -> None:
    """后台关闭 langgraph 流：sync close 可能等底层模型流，放 daemon 线程。"""
    try:
        gen.close()
    except Exception:
        pass


class WhiteResultInput(BaseModel):
    """White 的最终结果（finish_task 的结构化 schema）。"""
    status: Literal["done", "blocked", "failed"] = Field(
        default="done", description="done=完成 / blocked=被阻塞 / failed=失败")
    summary: str = Field(description="给主助手的结论与交接摘要（必填）")
    findings: list[str] = Field(default_factory=list, description="关键发现/产出")
    assumptions: list[str] = Field(default_factory=list, description="你做的假设")
    next_steps: list[str] = Field(default_factory=list, description="建议的下一步")


class WhiteStopMiddleware(AgentMiddleware):
    """用户停止 / 已提交结果 / 请求关闭 → 在安全点结束本轮。"""

    def __init__(self, white: "WhiteAgent") -> None:
        super().__init__()
        self.white = white

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        if (self.white.stop_event.is_set() or self.white.finished
                or self.white._close_requested):
            return {"jump_to": "end"}
        return None


class WhiteContextMiddleware(AgentMiddleware):
    """White 的简易上下文管理：超过预算 → 旧消息摘要 + 滑动窗口（无记忆树）。"""

    def __init__(self, session, white: "WhiteAgent") -> None:
        super().__init__()
        self.session = session
        self.white = white

    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        msgs = list(state.get("messages") or [])
        budget = self.white.context_budget()
        if len(msgs) <= KEEP_RECENT + 1:
            return None
        try:
            if SummarizationMiddleware._estimate_messages_tokens(msgs) <= budget:
                return None
        except Exception:
            return None
        # 保留最近窗口；不把切口落在 tool 结果中间
        start = len(msgs) - KEEP_RECENT
        while start < len(msgs) and getattr(msgs[start], "type", "") == "tool":
            start += 1
        if start <= 1:
            return None
        old = [m for m in msgs[1:start] if getattr(m, "id", None)]
        if not old:
            return None
        summary = self._summarize(old)
        removes = [RemoveMessage(id=m.id) for m in old]
        note = HumanMessage(
            content=f"[White 工作记忆] {summary}",
            metadata={"white_summary": True})
        self.white.note(f"上下文压缩：{len(old)} 条 → 摘要")
        return {"messages": removes + [note]}

    def _summarize(self, old: list) -> str:
        lines = []
        for m in old[-COMPACT_MAX_MSGS:]:
            content = content_text(getattr(m, "content", "") or "")
            tcs = getattr(m, "tool_calls", None) or []
            if tcs:
                content += " [调用: " + "、".join(str(t.get("name") or "?") for t in tcs) + "]"
            if content.strip():
                lines.append(f"[{getattr(m, 'type', '?')}] {content[:COMPACT_MSG_CHARS]}")
        transcript = "\n".join(lines)
        if not transcript.strip():
            return "（旧上下文没有可提炼的内容）"
        try:
            prompt = prompt_loader.render("agents/white_compact", transcript=transcript)
            r = self.session._get_summary_model().invoke([HumanMessage(content=prompt)])
            text = content_text(getattr(r, "content", "") or "").strip()
            if text:
                return text[:COMPACT_OUT_CHARS]
        except Exception:
            _logger.exception("[white] 上下文压缩失败，退回截断")
        return transcript[-COMPACT_OUT_CHARS:]


class WhiteAgent:
    """一个 White 实例（同一时间只有一个；关闭后由 session 丢弃）。"""

    def __init__(self, session, model=None, notify: bool = True) -> None:
        self.session = session
        self._model = model
        self.notify = notify
        self.id = WHITE_ID
        self.name = WHITE_NAME
        self.status = "idle"          # idle / running / closed
        self.task = ""
        self.messages: list = []      # 只存对话消息（system 由 create_agent 注入）
        self.result: dict | None = None
        self.stop_event = threading.Event()
        self.finished = False
        self._close_requested = False
        self._active_model = None     # 当前任务专用模型（独立 HTTP 客户端，停止时关闭）
        self._queue: list[tuple[str, bool]] = []
        self._thread = None
        self._lock = threading.RLock()
        self._started_at = 0.0
        self._finished_at = 0.0
        self._changes: list[int] = []
        self._last_text = ""
        self._push_status()

    # ── 对外状态 ─────────────────────────────────────────────

    def status_dict(self) -> dict:
        pending = sum(
            1 for a in self.session.pending_approvals.values()
            if a.get("agent") == WHITE_ID)
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "task": self.task,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "result": self.result,
            "pending_approvals": pending,
        }

    def context_budget(self) -> int:
        try:
            return max(10000, int(self.session.settings.get("white_context_tokens", 60000)))
        except (TypeError, ValueError):
            return 60000

    def _max_steps(self) -> int:
        try:
            return max(5, int(self.session.settings.get("white_max_steps", 30)))
        except (TypeError, ValueError):
            return 30

    def _timeout_seconds(self) -> int:
        try:
            return max(60, int(float(self.session.settings.get(
                "white_timeout_minutes", 10)) * 60))
        except (TypeError, ValueError):
            return 600

    def _push(self, event_type: str, **payload) -> None:
        try:
            self.session.push_event({"type": event_type, "agent": WHITE_ID, **payload})
        except Exception:
            pass

    def _push_status(self) -> None:
        self._push("white_status", **self.status_dict())

    def note(self, text: str) -> None:
        self._push("white_note", text=str(text))

    # ── 生命周期 ─────────────────────────────────────────────

    def dispatch(self, instruction: str, reuse: bool = True) -> dict:
        text = str(instruction or "").strip()
        if not text:
            return {"ok": False, "error": "empty_instruction"}
        with self._lock:
            if self.status == "running":
                self._queue.append((text, bool(reuse)))
                return {"ok": True, "queued": True, "status": self.status}
            if self.status == "closed":
                return {"ok": False, "error": "closed"}
            self._prepare(text, bool(reuse))
            self._thread = threading.Thread(
                target=self._run, name="planner-white", daemon=True)
            self._thread.start()
            return {"ok": True, "queued": False, "status": self.status}

    def _prepare(self, text: str, reuse: bool) -> None:
        if not reuse:
            self.messages = []
            self.result = None
        self.task = text
        self.finished = False
        self.stop_event.clear()
        self._close_requested = False
        self._last_text = ""
        self._changes = []
        self.messages.append(HumanMessage(
            content=text, id=uuid.uuid4().hex,
            metadata={"white_instruction": True,
                      "ts": time.strftime("%Y-%m-%d %H:%M:%S")}))
        self.status = "running"
        self._started_at = time.time()
        self._finished_at = 0.0
        self._push_status()

    def _abort_model(self) -> None:
        """立即关闭当前任务的模型 HTTP 客户端，中断在途 API 请求。

        White 使用独立模型实例（见 _run_once），关闭不会影响主助手。正在
        执行的写文件工具会先写完；模型返回的异常由 _run_once 按“停止”收尾。
        """
        model = self._active_model
        self._active_model = None
        if model is None:
            return
        if model is self._model:
            self._model = None
        root = getattr(model, "root_client", None)
        if root is None:
            return
        try:
            root.close()
            _logger.info("[white] 已关闭模型 HTTP 客户端，在途请求立即中断")
        except Exception:
            _logger.exception("[white] 关闭模型客户端失败")

    def stop(self) -> dict:
        if self.status != "running":
            return {"ok": True, "status": self.status, "note": "没有运行中的任务"}
        with self._lock:
            self._queue.clear()
            self.stop_event.set()
        self.note("收到停止请求…")
        self._abort_model()
        return {"ok": True, "status": "stopping"}

    def close(self) -> dict:
        self._close_requested = True
        with self._lock:
            self._queue.clear()
            self.stop_event.set()
        self._abort_model()
        if self.status != "running":
            self.status = "closed"
            self.session.drop_white(self)
            self._push_status()
            return {"ok": True, "status": "closed"}
        self.note("收到关闭请求，停止后关闭…")
        return {"ok": True, "status": "closing"}

    def _run(self) -> None:
        set_current_agent(WHITE_ID)
        while True:
            try:
                self._run_once()
            except Exception as exc:
                _logger.exception("[white] 运行异常")
                self.status = "idle"
                self.result = {
                    "status": "failed",
                    "summary": f"White 运行异常：{exc}",
                    "findings": [], "assumptions": [], "next_steps": [],
                    "files": [],
                }
            with self._lock:
                if self._close_requested:
                    self.status = "closed"
                    break
                if self._queue:
                    text, reuse = self._queue.pop(0)
                    self._prepare(text, reuse)
                    continue
                self.status = "idle"
                break
        self._finished_at = time.time()
        self._persist()
        self._push_status()
        if self.status == "closed":
            self.session.drop_white(self)
        else:
            self._notify_main()

    # ── 单轮执行 ─────────────────────────────────────────────

    def _build_tools(self) -> list:
        st = self.session.settings
        ws = str(st.get("workspace_root") or "").strip()
        base = {t.name: t for t in build_tools(self.session)}
        names = {"web_search", "fetch_web"}
        tools = []
        if ws:
            from pathlib import Path as _P

            from .workspace import (effective_cwd as _eff, is_blocked as _blocked,
                                    resolve_target as _resolve_target, is_within as _within)
            ws_path = _P(ws)
            cwd = _eff(ws_path, str(st.get("workspace_cwd") or "")) or ws_path
            base_read = base.get("read_file")
            base_list = base.get("list_dir")

            def _inside(raw: str):
                """White 专用路径解析：相对 cwd，且必须落在工作区内。"""
                p = _P(str(raw or "")).expanduser()
                if not p.is_absolute():
                    p = _resolve_target(cwd, str(raw or ""))
                try:
                    p = p.resolve()
                except OSError:
                    pass
                if not _within(ws_path, p):
                    return None, f"（White 只允许在工作区内操作，已拒绝：{p}）"
                if _blocked(p):
                    return None, f"（该路径在保护名单内，已拒绝：{p}）"
                return p, None

            @tool(parse_docstring=True)
            def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
                """读取工作区内的文本文件（相对路径按当前工作目录解析；不能越界）。

                Args:
                    path: 文件路径（相对当前工作目录或工作区内绝对路径）
                    offset: 从第几行开始读（1 起始）
                    limit: 本次最多读取的行数（1~1500）
                """
                if base_read is None:
                    return "（读取工具不可用）"
                p, err = _inside(path)
                if err:
                    return err
                return base_read.invoke({"path": str(p), "offset": offset,
                                        "limit": limit})

            @tool(parse_docstring=True)
            def list_dir(path: str) -> str:
                """列出工作区内的目录内容（相对路径按当前工作目录解析；不能越界）。

                Args:
                    path: 目录路径（相对当前工作目录或工作区内绝对路径；空 = 当前工作目录）
                """
                if base_list is None:
                    return "（目录工具不可用）"
                p, err = _inside(path or ".")
                if err:
                    return err
                return base_list.invoke({"path": str(p)})

            names |= {"get_cwd", "apply_patch", "write_file", "edit_file",
                      "create_dir", "move_file", "delete_file", "glob", "grep"}
            tools += [read_file, list_dir]
        tools += [t for n, t in base.items() if n in names]
        tools.append(self._finish_tool())
        return tools

    def _finish_tool(self):
        white = self

        @tool(args_schema=WhiteResultInput)
        def finish_task(status: str = "done", summary: str = "",
                        findings: list[str] | None = None,
                        assumptions: list[str] | None = None,
                        next_steps: list[str] | None = None) -> str:
            """提交最终结果并结束 White 的任务（必须调用）。"""
            if not str(summary).strip():
                return "（summary 不能为空：请简要写出结论/交接内容。）"
            white.result = {
                "status": str(status or "done"),
                "summary": str(summary).strip(),
                "findings": [str(x) for x in (findings or [])],
                "assumptions": [str(x) for x in (assumptions or [])],
                "next_steps": [str(x) for x in (next_steps or [])],
            }
            white.finished = True
            return "（结果已记录，任务结束。）"

        return finish_task

    def _on_timeout(self) -> None:
        """总时限看门狗：即使模型请求卡住，也主动关闭客户端收尾。"""
        if self.status != "running" or self.finished:
            return
        self.stop_event.set()
        self.note("White 超时，已停止。")
        self._abort_model()

    def _run_once(self) -> None:
        # White 使用独立模型实例：停止时只关闭自己的 root_client，不影响主助手。
        model = self._model or self.session.build_llm()
        self._active_model = model
        agent = create_agent(
            model=model,
            tools=self._build_tools(),
            system_prompt=prompt_loader.load("agents/white"),
            middleware=[
                WhiteStopMiddleware(self),
                WhiteContextMiddleware(self.session, self),
                ApprovalMiddleware(self.session),
                LoggingMiddleware(self.session),
                ModelCallLimitMiddleware(run_limit=self._max_steps(),
                                         exit_behavior="end"),
            ],
            state_schema=PlannerState,
        )
        seen = {getattr(m, "id", None) or id(m) for m in self.messages}
        final = None
        current_chunks: list[str] = []
        current_msg_id = None
        deadline = time.time() + self._timeout_seconds()
        timer = threading.Timer(max(1.0, deadline - time.time()), self._on_timeout)
        timer.daemon = True
        timer.start()
        stream = None
        try:
            stream = agent.stream({"messages": list(self.messages)},
                                  stream_mode=["messages", "values"])
            for kind, data in stream:
                if self.stop_event.is_set() or self._close_requested:
                    break
                if time.time() > deadline:
                    self._on_timeout()
                    break
                if kind == "messages":
                    chunk, metadata = data
                    if (metadata or {}).get("langgraph_node") != "model":
                        continue
                    reasoning = ((getattr(chunk, "additional_kwargs", None) or {})
                                 .get("reasoning_content") or "")
                    if reasoning:
                        self._push("thinking_stream", content=str(reasoning),
                                   msg_id=current_msg_id or "")
                    content = content_text(getattr(chunk, "content", "") or "")
                    if content:
                        if current_msg_id is None:
                            current_msg_id = uuid.uuid4().hex[:12]
                        current_chunks.append(content)
                        self._push("text_stream", content=content,
                                   msg_id=current_msg_id)
                elif kind == "values":
                    msgs = data.get("messages")
                    if msgs is None:
                        continue
                    final = msgs
                    for m in msgs:
                        mid = getattr(m, "id", None) or id(m)
                        if mid in seen:
                            continue
                        seen.add(mid)
                        self._emit_msg(m)
                    if current_chunks:
                        full = "".join(current_chunks)
                        self._last_text = full
                        self._push("text", content=full, msg_id=current_msg_id)
                        current_chunks = []
                        current_msg_id = None
        except Exception as exc:
            if not (self.stop_event.is_set() or self._close_requested):
                raise
            # 停止/超时主动关闭了模型客户端 → httpx ReadError 等，按停止收尾。
            _logger.info("[white] 停止请求已中断模型调用: %s", exc)
        finally:
            timer.cancel()
            if stream is not None:
                threading.Thread(target=_close_stream_bg, args=(stream,),
                                 name="planner-white-stream-close",
                                 daemon=True).start()
            self._active_model = None
        if final is not None:
            self.messages = final
        if current_chunks:
            full = "".join(current_chunks)
            self._last_text = full
            self._push("text", content=full, msg_id=current_msg_id)
        # 结果兜底
        if not self.result:
            stopped = self.stop_event.is_set() or self._close_requested
            self.result = {
                "status": "blocked" if stopped else ("done" if self._last_text else "failed"),
                "summary": (self._last_text or ("White 被停止了。" if stopped
                                                else "White 没有产出结果。")),
                "findings": [], "assumptions": [], "next_steps": [],
            }
        self._changes.extend(self.session.take_changes(WHITE_ID))
        try:
            self.result["files"] = self.session.changed_files_for_ids(self._changes)
        except Exception:
            self.result["files"] = []
        self.result["usage"] = self._usage()
        self.result["elapsed_seconds"] = int(time.time() - self._started_at)
        self.finished = True
        self._push("white_result", result=self.result)
        self._persist()

    def _emit_msg(self, m) -> None:
        mtype = getattr(m, "type", None)
        if mtype == "ai":
            for tc in (getattr(m, "tool_calls", None) or []):
                name = tc.get("name", "?")
                if name == "finish_task":
                    continue
                self._push("tool_call", id=tc.get("id", ""), name=name,
                           args=tc.get("args", {}) or {})
        elif mtype == "tool":
            name = getattr(m, "name", "") or ""
            if name == "finish_task":
                return
            ids = self.session.take_changes(WHITE_ID, tool=name)
            self._changes.extend(ids)
            ak = getattr(m, "additional_kwargs", None)
            if ids and isinstance(ak, dict):
                ak["change_ids"] = ids
            diff = self.session.change_diff(ids, tool=name) if ids else None
            self._push("tool_result",
                       id=getattr(m, "tool_call_id", ""),
                       content=str(content_text(getattr(m, "content", "") or ""))[:8000],
                       diff=diff)

    def _usage(self) -> dict:
        inp = out = hit = reason = 0
        for m in self.messages:
            um = getattr(m, "usage_metadata", None) or {}
            inp += int(um.get("input_tokens") or 0)
            out += int(um.get("output_tokens") or 0)
            details = um.get("input_token_details") or {}
            hit += int(details.get("cache_read") or 0)
            od = um.get("output_token_details") or {}
            reason += int(od.get("reasoning") or 0)
        return {"input_tokens": inp, "output_tokens": out,
                "cache_read_tokens": hit, "reasoning_tokens": reason}

    # ── 通知 / 持久化 / 历史 ─────────────────────────────────

    def _notify_main(self) -> None:
        if not self.notify:
            return
        r = self.result or {}
        files = "；".join(str(f.get("rel") or f.get("path") or "")
                          for f in (r.get("files") or []) if f)
        text = prompt_loader.render(
            "runtime/white_finished",
            status=r.get("status") or "done",
            summary=str(r.get("summary") or "")[:600],
            files=(f" 改动文件：{files}。" if files else ""),
            stopped=(" 用户已停止这个任务。" if self.stop_event.is_set() else ""))
        session = self.session

        def _worker() -> None:
            deadline = time.time() + 120
            while time.time() < deadline:
                if getattr(session, "_closing", False):
                    return
                with session.chat_lock:
                    if not session._generating:
                        session._receive(text, trigger=True, injected=True,
                                         kind="white")
                        session._spawn_worker("white")
                        return
                time.sleep(1)
            session._receive(text, trigger=False, injected=True, kind="white")

        threading.Thread(target=_worker, name="white-notify", daemon=True).start()

    def _persist(self) -> None:
        try:
            path = Path(self.session.data_root) / "agents" / "white.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "status": self.status,
                "task": self.task,
                "result": self.result,
                "messages": messages_to_dict(self.messages),
                "updated_at": time.time(),
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            _logger.debug("[white] 持久化失败", exc_info=True)

    def history(self) -> list[dict]:
        out: list[dict] = []
        for m in self.messages:
            mtype = getattr(m, "type", None)
            meta = getattr(m, "metadata", None) or {}
            content = content_text(getattr(m, "content", "") or "")
            if mtype == "human":
                if meta.get("white_instruction"):
                    out.append({"role": "user", "content": content})
                continue
            if mtype == "ai":
                reasoning = ((getattr(m, "additional_kwargs", None) or {})
                             .get("reasoning_content") or "")
                if reasoning:
                    out.append({"role": "thinking", "content": reasoning})
                if content:
                    out.append({"role": "assistant", "content": content})
                for tc in (getattr(m, "tool_calls", None) or []):
                    name = tc.get("name", "?")
                    if name == "finish_task":
                        continue
                    out.append({"role": "tool_call", "id": tc.get("id", ""),
                                "name": name, "args": tc.get("args", {}) or {}})
            elif mtype == "tool":
                name = getattr(m, "name", "") or ""
                if name == "finish_task":
                    continue
                ak = getattr(m, "additional_kwargs", None) or {}
                ids = [int(i) for i in (ak.get("change_ids") or [])]
                diff = self.session.change_diff(ids, tool=name) if ids else None
                out.append({"role": "tool_result",
                            "id": getattr(m, "tool_call_id", ""),
                            "content": content[:8000], "diff": diff})
        if self.result:
            out.append({"role": "result", "result": self.result})
        return out
