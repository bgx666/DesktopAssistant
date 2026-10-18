"""planner 业务中间件（LangChain AgentMiddleware，移植自 yaya backend）。

钩子语义：
- node-style（before_agent / before_model / after_model）：顺序执行，返回 dict 合并进
  state（走 reducer），支持 {"jump_to": "end"} 提前跳转；
- wrap-style（wrap_model_call / wrap_tool_call）：包裹每次调用，可短路/重试/转换。

中间件链（agent.py 中按序）：
- DndGuardMiddleware      before_agent   免打扰时段且非玩家触发 → 跳 end
- AgendaSnapshotMiddleware  before_agent   议程有变化 → 注入 [当前议程]（今天+未来 7 天日历）
- PlayerPriorityMiddleware before_model   玩家消息在 _inbox → 跳 end（让位）
- NudgeMiddleware         after_model 计数 + before_model 第 5 轮注入「走神」提示
- HeartbeatTrackMiddleware wrap_tool_call 记录 + before_model 跳转（heartbeat 即停）
- ApprovalMiddleware      wrap_tool_call 代码工作区审批（写/区外读挂起，黑名单硬拒）
- LoggingMiddleware       wrap_*        每轮 LLM/工具调用写 jsonl 日志

ReAct 循环：**用 create_agent 内建的原生循环**（model → tools → model → …），
模型不再返回工具调用就自然结束本轮。中间件只负责"提前跳出"的几种情况
（玩家让位 / heartbeat / 用户停止 / 免打扰），**不主动截断工具→模型的回路**。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.messages import HumanMessage, RemoveMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from typing_extensions import NotRequired

from .workspace import (
    PermissionStore,
    READ_TOOLS,
    RUN_TOOLS,
    WRITE_TOOLS,
    blocked_command_reason,
    command_prefix,
    effective_cwd,
    is_blocked,
    is_within,
    rel_posix,
    tool_targets,
)

_logger = logging.getLogger("planner.middleware")


class PlannerState(AgentState):
    """planner agent 的 state 扩展字段。"""

    model_call_count: NotRequired[int]
    set_heartbeat_called: NotRequired[bool]
    plan_injected: NotRequired[bool]


# ── 免打扰守卫 ───────────────────────────────────────────────

class DndGuardMiddleware(AgentMiddleware):
    """before_agent：免打扰时段内，自主触发（非玩家消息）→ 跳 end。

    玩家消息与 nudge（用户主动戳悬浮球）永远不受限——用户主动交互肯定要回。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: PlannerState, runtime: Runtime) -> dict | None:
        if self.session.in_dnd() and self.session.current_trigger not in ("player", "nudge"):
            _logger.info("[dnd] 免打扰时段，跳过自主触发")
            self.session.push_log("（免打扰时段，暂不打扰。）")
            return {"jump_to": "end"}
        return None


# ── 议程快照注入（数据源＝日历）──────────────────────────────

class AgendaSnapshotMiddleware(AgentMiddleware):
    """before_agent：议程指纹变化时注入 [当前议程]（今天+未来 7 天的日历）。

    每次生成最多注入一次；内容无变化时不注入（指纹去重，避免每轮重复占 token）。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: PlannerState, runtime: Runtime) -> dict | None:
        text = self.session._agenda_snapshot_text()
        if text is None:
            return None
        return {"messages": [HumanMessage(content=text)]}


# ── 屏幕截图注入 ─────────────────────────────────────────────

class ScreenShotInjectMiddleware(AgentMiddleware):
    """待注入图片（capture_screen 截屏 / view_image 读图）：以 user 消息注入对话。

    工具结果（tool 消息）不能携带图片块（DeepSeek 400），但截屏/读图的目的就是让模型
    "看到"画面——所以图片由工具存入 session._pending_screenshots（条目为
    `{url, text, viewed?}`），在本轮下一次模型调用前（before_model）注入 state。
    图片由此进入对话上下文：
    - 可被后续轮次/后续对话反复查看（同一张图随 buffer 常驻）；
    - 与对话共用前缀缓存（后续相同前缀的请求命中 KV cache，不再重复计费图片 token）；
    - `viewed` 写入消息 additional_kwargs（白名单转换不发回端点）→ view_image 去重用，
      重启后随 buffer_state 恢复；被压缩/移除后图片自然离开上下文、允许重新读入。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        pending = getattr(self.session, "_pending_screenshots", None)
        if not pending:
            return None
        msgs = []
        for entry in pending:
            if isinstance(entry, dict):
                url = str(entry.get("url") or "")
                text = str(entry.get("text")
                           or "（这是屏幕截图，请查看画面内容后回答。）")
                kwargs: dict = {}
                if entry.get("viewed"):
                    kwargs["viewed_image"] = entry["viewed"]
            else:   # 兼容纯 URL 字符串（旧格式/外部注入）
                url, text, kwargs = str(entry), "（这是屏幕截图，请查看画面内容后回答。）", {}
            if not url:
                continue
            msgs.append(HumanMessage(
                content=[
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
                additional_kwargs=kwargs))
        self.session._pending_screenshots = []
        if not msgs:
            return None
        return {"messages": msgs}


# ── 玩家消息优先 ─────────────────────────────────────────────

class PlayerPriorityMiddleware(AgentMiddleware):
    """玩家消息优先：_inbox 有排队消息时提前结束自主回合（让位给玩家）。

    必须在 before_model 跳转（上一批工具已执行完）：create_agent 节点顺序是
    model → tools → model…，若用 after_model 跳转，模型刚返回的 tool_calls 会
    跳过执行 → state 末尾孤儿 assistant(tool_calls) → DeepSeek 400。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        with self.session.buffer_lock:
            if self.session._inbox:
                return {"jump_to": "end"}
        return None


# ── 走神提示 ─────────────────────────────────────────────────

class NudgeMiddleware(AgentMiddleware):
    """after_model 递增 model_call_count；before_model 在第 5 轮后注入收尾提示。

    原生多轮 ReAct 下这个计数才开始真正有意义：一轮对话里模型可以连续
    「调工具 → 看结果 → 再调工具」很多次，这里的第 5 轮提示是唯一温和的
    软上限（硬上限是 agent.py 的 ModelCallLimitMiddleware run_limit）。
    """

    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        if state.get("model_call_count", 0) == 5:
            return {"messages": [HumanMessage(content="（你已经忙了一小会儿了，把最重要的事做完就歇一下吧。）")]}
        return None

    def after_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        return {"model_call_count": state.get("model_call_count", 0) + 1}


# ── heartbeat 即停止 ─────────────────────────────────────────

class HeartbeatTrackMiddleware(AgentMiddleware):
    """wrap_tool_call 记录 heartbeat 调用；before_model 调过 → jump_to:"end"。

    关键语义：create_agent 的循环在工具执行后总会再调模型（停止条件是"模型不再
    返回工具调用"），必须在 before_model 跳转——若用 after_model，heartbeat 标记
    会在下一次模型调用后立即跳转，导致该轮其他工具（如 set_do_not_disturb）
    从未执行。返回 Command 时必须把工具结果消息放进 update["messages"]，
    否则 ToolMessage 丢失 → DeepSeek 400。
    """

    def wrap_tool_call(self, request, handler: Callable):
        result = handler(request)
        if request.tool_call["name"] == "heartbeat":
            return Command(update={"set_heartbeat_called": True, "messages": [result]})
        return result

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        if state.get("set_heartbeat_called"):
            return {"jump_to": "end"}
        return None


# ── 用户正在输入提示 ───────────────────────────────────────

class TypingHintMiddleware(AgentMiddleware):
    """wrap_model_call：用户输入框非空时，每次 LLM 调用前在上下文末尾临时追加
    一句提示（只影响本次调用，不落 state / buffer）。"""

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def wrap_model_call(self, request, handler: Callable):
        if self.session.typing:
            request = request.override(messages=[
                *request.messages,
                HumanMessage(
                    content="（当前用户正在输入文字，可能马上要发消息。"
                            "如果你生成时用户可能在输入，请别说太多，简短收尾。）",
                ),
            ])
        return handler(request)


# ── 停止请求 ─────────────────────────────────────────────

class StopRequestMiddleware(AgentMiddleware):
    """用户点「停止」→ session._stop_requested 置位 → 在安全点跳 end。

    只在 before_model 跳转（与 PlayerPriority 同理：after_model 跳转会跳过
    刚返回的 tool_calls 执行 → 孤儿 assistant(tool_calls) → DeepSeek 400）。
    若上一轮模型刚返回 tool_calls 还没执行，本轮不能跳（等工具执行完，
    下一轮 before_model 再停）。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        if not self.session._stop_requested:
            return None
        msgs = list(state.get("messages") or [])
        if msgs and getattr(msgs[-1], "tool_calls", None):
            return None   # 工具还没执行完，下一轮再停
        return {"jump_to": "end"}


# ── 日志 ─────────────────────────────────────────────────────

class LoggingMiddleware(AgentMiddleware):
    """wrap_model_call / wrap_tool_call：每轮 LLM/工具调用写 jsonl 日志。"""

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @staticmethod
    def _shorten(content: str, max_len: int = 120) -> str:
        if not content:
            return ""
        if len(content) <= max_len:
            return content
        return content[: max_len - 10] + " ... " + content[-10:]

    def wrap_model_call(self, request, handler: Callable):
        # 记录本次模型调用耗时：排查"这次怎么回得这么慢"时，一眼区分
        # 「模型/网络本身慢」与「我们自己被锁/压缩挡住了」——两者日志形状完全不同。
        # 流式模型下 handler 在响应建立时返回，所以 ms ≈ 首包时间（体感延迟就是它）。
        t0 = time.perf_counter()
        response = handler(request)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        try:
            ai = response.result
            if isinstance(ai, list):
                ai = ai[0] if ai else None
            tcs = [t.get("name") for t in (ai.tool_calls or [])] if ai is not None else []
            self.session._save_log("llm_call", json.dumps({
                "msgs": len(request.messages),
                "ms": elapsed_ms,
                "content": self._shorten((ai.content or "") if ai is not None else ""),
                "tool_calls": tcs,
            }, ensure_ascii=False))
        except Exception:
            _logger.exception("[middleware] llm 日志失败")
        return response

    def wrap_tool_call(self, request, handler: Callable):
        name = request.tool_call["name"]
        args = json.dumps(request.tool_call.get("args", {}), ensure_ascii=False)
        t0 = time.perf_counter()
        try:
            result = handler(request)
        finally:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            try:
                # 工具耗时同样入日志（截图/搜索/读大文件这类慢工具一眼可见）
                self.session._save_log("tool_call", f"{name}({args}) [{elapsed_ms}ms]")
            except Exception:
                _logger.exception("[middleware] tool 日志失败")
        return result


# ── 审批制（代码工作区）─────────────────────────────────────

class ApprovalMiddleware(AgentMiddleware):
    """写操作 / 工作区外读 → 挂起等用户确认；黑名单硬拒；超时默认拒绝。

    规则（docs/代码Agent-实施计划.md 三、3.3）：
    - 黑名单命中 → 硬拒（返回提示串，不弹审批）；
    - 工作区内只读 → 放行；
    - 写操作 / 工作区外读 → 查 permissions.json，未命中则挂起审批
      （allow_once 放行 / always 落规则后放行 / deny 与超时默认拒绝）；
    - 无 workspace：代码工具未注册；只读工具保持原有自由读（仍过黑名单）。

    挂起由 session.request_approval 实现（Event 分片等待，用户点停止/关窗立即唤醒）。
    拒绝时返回一条 ToolMessage 顶替工具结果（tool_call_id 对齐，避免孤儿 tool_calls）。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def wrap_tool_call(self, request, handler: Callable):
        name = request.tool_call.get("name") or ""
        if name not in WRITE_TOOLS and name not in READ_TOOLS:
            return handler(request)
        verdict, message = self._authorize(name, request.tool_call.get("args") or {})
        if verdict != "allow":
            return ToolMessage(content=message,
                               tool_call_id=request.tool_call.get("id") or "",
                               name=name)
        return handler(request)

    def _authorize(self, name: str, args: dict) -> tuple[str, str]:
        """返回 (allow|deny, 拒绝原因)。权限矩阵见 docs/代码Agent-界面升级方案.md 2.2。"""
        st = self.session.settings
        ws_raw = str(st.get("workspace_root") or "").strip()
        try:
            workspace = Path(ws_raw).resolve() if ws_raw else None
        except OSError:
            workspace = None
        cwd = effective_cwd(workspace, str(st.get("workspace_cwd") or ""))
        mode = str(st.get("permission_mode") or "ask").strip().lower()
        # 命令执行：不走路径逻辑（硬黑名单 → 只读拒 → 规则 → 逐条审批）
        if name in RUN_TOOLS:
            return self._authorize_command(name, args)
        targets = tool_targets(name, args, cwd)
        if not targets:
            return "deny", "（路径参数缺失，已取消该操作。）"
        # 黑名单：任何模式都硬拒（读也拒——防 .env / 私钥泄进对话）
        for p in targets:
            if is_blocked(p):
                return "deny", f"（该路径在保护名单内，已拒绝：{p}）"
        # 自身落盘的工具结果（data/tool_results/）→ 读免审批（卸载回来的大输出）
        try:
            offload_dir = Path(str(self.session.data_root)) / "tool_results"
            if name in READ_TOOLS and all(is_within(offload_dir, p) for p in targets):
                return "allow", ""
        except (TypeError, AttributeError):
            pass
        if workspace is None:
            if name in WRITE_TOOLS:
                return "deny", "（尚未设置代码工作区，写操作已取消。）"
            return "allow", ""            # 无工作区：只读保持原有自由读
        # 只读模式：禁一切写（cwd 内也禁）
        if mode == "read_only" and name in WRITE_TOOLS:
            return "deny", "（当前为只读模式，已取消。可在标题栏切换权限模式。）"
        store = PermissionStore(self.session.data_root)
        pending: list[tuple[str, str]] = []       # [(tool, 工作区相对路径)] 待审批
        for p in targets:
            inside = is_within(workspace, p)
            rel = rel_posix(workspace, p)
            if inside and name in READ_TOOLS:
                continue                              # 区内只读 → 放行
            if (not inside and name in READ_TOOLS
                    and not st.get("code_read_outside_workspace", False)):
                return "deny", (f"（工作区外的读取未开启，已取消：{p}。"
                                "如需允许，请在设置中开启「允许工作区外读取」。）")
            if (mode == "auto" and name in WRITE_TOOLS and inside
                    and cwd is not None and is_within(cwd, p)):
                continue                              # Auto：当前目录内写自动放行
            if store.match(name, rel):
                continue                              # 命中 always 规则
            pending.append((name, rel))
        if not pending:
            return "allow", ""
        preview = self._approval_preview(name, args)
        decision = self.session.request_approval(
            name, [str(p) for p in targets],
            preview=preview,
            scopes=self._approval_scopes(pending),
            cwd=str(cwd) if cwd is not None else "",
        )
        if decision.startswith("always"):
            for tool, rel in pending:
                store.add(tool, self._scope_glob(rel, decision))
            return "allow", ""
        if decision == "allow_once":
            return "allow", ""
        if decision == "timeout":
            return "deny", "（审批超时，已自动取消该操作。）"
        if decision == "stop":
            return "deny", "（用户中断了本次生成，操作已取消。）"
        return "deny", "（用户拒绝了此操作，已取消。）"

    @staticmethod
    def _scope_glob(rel: str, decision: str) -> str:
        """按用户选择的范围，把「工作区相对路径」转成规则 glob。"""
        rel = str(rel or "").strip()
        if decision == "always_workspace":
            return "**"
        if decision == "always_dir" and "/" in rel:
            return rel.rsplit("/", 1)[0] + "/**"
        return rel or "**"

    def _authorize_command(self, name: str, args: dict) -> tuple[str, str]:
        """命令执行：硬黑名单 → 只读模式拒 → 命中命令规则放行 → **逐条审批**。

        Ask 与 Auto 模式下命令一律要用户确认（命令能逃出工作区边界，不能像写文件那样
        按 cwd 自动放行）；只读模式直接拒绝。例外只有用户显式落下的命令规则
        （「记住并允许」的 仅此命令 / 以 X 开头）。
        """
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return "deny", "（命令为空，已取消。）"
        reason = blocked_command_reason(cmd)
        if reason:
            return "deny", f"（命令命中保护规则「{reason}」，已拒绝：{cmd[:160]}）"
        mode = str(self.session.settings.get("permission_mode") or "ask").strip().lower()
        if mode == "read_only":
            return "deny", "（当前为只读模式，已取消。可在标题栏切换权限模式。）"
        store = PermissionStore(self.session.data_root)
        if store.match_command(name, cmd):
            return "allow", ""
        cwd = self.session.effective_cwd()
        decision = self.session.request_approval(
            name, [],
            preview=cmd,
            scopes=self._command_scopes(cmd),
            cwd=str(cwd) if cwd is not None else "",
        )
        if decision == "always_cmd":
            store.add_command(name, cmd)
            return "allow", ""
        if decision == "always_cmd_prefix":
            store.add_command(name, command_prefix(cmd))
            return "allow", ""
        if decision == "allow_once":
            return "allow", ""
        if decision == "timeout":
            return "deny", "（审批超时，已自动取消该命令。）"
        if decision == "stop":
            return "deny", "（用户中断了本次生成，命令已取消。）"
        return "deny", "（用户拒绝了此命令，已取消。）"

    @staticmethod
    def _command_scopes(cmd: str) -> list[dict]:
        """命令的授权范围候选：仅此命令 / 以「首词」开头的命令（如 pytest）。"""
        scopes = [{"kind": "cmd", "label": "仅此命令", "glob": cmd}]
        head = str(cmd or "").strip().split()
        if head:
            scopes.append({"kind": "cmd_prefix",
                           "label": f"以「{head[0]}」开头的命令", "glob": head[0] + " *"})
        return scopes

    @staticmethod
    def _approval_scopes(pending: list[tuple[str, str]]) -> list[dict]:
        """候选授权范围（弹窗单选：仅此文件 / 此目录 / 整个工作区）。"""
        rel = pending[0][1] if pending else ""
        dir_part = rel.rsplit("/", 1)[0] if "/" in rel else ""
        scopes = [{"kind": "file", "label": f"仅此文件（{rel or '?'}）", "glob": rel or "**"}]
        if dir_part:
            scopes.append({"kind": "dir", "label": f"此目录（{dir_part}/）",
                           "glob": dir_part + "/**"})
        scopes.append({"kind": "workspace", "label": "整个工作区", "glob": "**"})
        return scopes

    @staticmethod
    def _approval_preview(name: str, args: dict) -> str:
        """审批预览文本：run_command → 命令原文；edit_file → 统一 diff；write_file → 内容摘要；
        apply_patch → 补丁原文（截断）。"""
        import difflib
        try:
            if name == "run_command":
                return str(args.get("command") or "")[:2000]
            if name == "edit_file":
                diff = difflib.unified_diff(
                    str(args.get("old_string") or "").splitlines(),
                    str(args.get("new_string") or "").splitlines(),
                    lineterm="", n=1)
                lines = list(diff)
                return "\n".join(lines[:60]) + ("\n…（更多差异已省略）"
                                                if len(lines) > 60 else "")
            if name == "write_file":
                content = str(args.get("content") or "")
                head = "\n".join(content.strip().splitlines()[:5])
                return f"共 {len(content)} 字符\n{head}"[:600]
            if name == "apply_patch":
                patch = str(args.get("patch") or "")
                lines = patch.splitlines()
                return "\n".join(lines[:80]) + ("\n…（补丁较长已截断）"
                                                if len(lines) > 80 else "")
        except Exception:
            return ""
        return ""


# ── 压缩 + 记忆树 ────────────────────────────────────────────

SUMMARIZE_KEEP_MESSAGES = 20
SUMMARIZE_TOKEN_BUDGET = 100000
LEVEL_COMPACT_THRESHOLD = 8
BRANCHING_FACTOR = 4
#: 整批一次压缩的估算 token 上限：批次超过它才直接走分片（否则"整批优先"，
#: 一次压缩 = 一个 L0 节点——用户预期；超窗失败也会自动回退分片兜底）
_ONE_SHOT_MAX_TOKENS = 300000


def should_compress_raw(raw_msgs: list, keep: int, token_budget: int) -> bool:
    """是否需要叶子压缩：**只看原始消息估算 token ≥ 预算**（条数不参与判定）。

    token 估算与 session.context_stats 一致（chars × 1.2，中文为主，偏保守）。
    至少 2 条原始消息才可能圈出批次；正常保留 keep 条，连 keep 条都超预算时
    只保留最后 1 条（见 compress_snapshot 的 keep_n 兜底）。
    """
    if len(raw_msgs) <= 1:
        return False
    if token_budget <= 0:
        return False
    chars = sum(len(str(getattr(m, "content", "") or "")) for m in raw_msgs)
    return int(chars * 1.2) >= token_budget

from pydantic import BaseModel, Field  # noqa: E402


#: 记忆节点 schema 版本（1 = 旧画像型；2 = 任务型，内容存 meta.fields）
SCHEMA_VERSION = 2

#: 节点上的保护标记：常驻在对话上下文的节点文本里；节点压缩时目标节点去掉它
NODE_MARKER = "[已压缩 · 不需要压缩]"


class TaskInfo(BaseModel):
    """任务状态：目标 / 进度 / 待办 / 下一步 / 阻塞。"""
    goal: str = Field(default="", description="当前总体目标（没有则留空）")
    status: str = Field(default="", description="当前状态：做到哪了（一句话）")
    todo: list[str] = Field(default_factory=list, description="未完成的待办")
    next_steps: list[str] = Field(default_factory=list, description="计划的下一步")
    blockers: list[str] = Field(default_factory=list, description="阻塞/风险")


class DecisionInfo(BaseModel):
    what: str = Field(default="", description="决定了什么")
    why: str = Field(default="", description="为什么这样定（务必写清理由，它比结论更常被后续需要）")


class LessonError(BaseModel):
    symptom: str = Field(default="", description="症状：报错/异常现象")
    cause: str = Field(default="", description="原因")
    fix: str = Field(default="", description="解决办法")


class LessonsInfo(BaseModel):
    errors: list[LessonError] = Field(default_factory=list,
                                      description="错误经验：症状/原因/修法")
    patterns: list[str] = Field(default_factory=list,
                                description="成功经验：可复用的做法/模式")


class ContextInfo(BaseModel):
    cwd: str = Field(default="",
                     description="执行这段对话时所在的工作目录（由系统填写，你不用填）")
    env: list[str] = Field(default_factory=list,
                           description="环境与配置（路径、版本、依赖、设置）")
    commands: list[str] = Field(default_factory=list,
                                description="关键命令（可逐字保留）")
    artifacts: list[str] = Field(default_factory=list,
                                 description="文件/符号引用（路径、函数/字段名）")
    constraints: list[str] = Field(default_factory=list,
                                   description="必须遵守的约束（安全/规范/禁止事项；不随压缩丢弃）")


class MemoryNodeOutput(BaseModel):
    """压缩节点的 LLM 结构化输出（叶子与父节点共用；任务型 schema）。

    字段演进约定：新增字段改本 model + 提示词即可，存储零迁移
    （内容存 meta.fields JSON；extra=ignore 丢弃未知字段）。
    """

    model_config = {"extra": "ignore"}

    summary: str = Field(description="这段时间发生了什么：事件/进展/决定与对话进程（叙述）")
    task: TaskInfo = Field(default_factory=TaskInfo)
    decisions: list[DecisionInfo] = Field(default_factory=list)
    lessons: LessonsInfo = Field(default_factory=LessonsInfo)
    context: ContextInfo = Field(default_factory=ContextInfo)
    user: list[str] = Field(
        default_factory=list,
        description="用户性格/偏好/习惯（含工作偏好：常用工具、代码风格、沟通习惯）；没有则留空数组")
    meta: dict = Field(default_factory=dict,
                       description="节点元信息：schema_version 与未来扩展字段")

    def fields_payload(self) -> dict:
        """落树字段载荷（不含 summary——它存 nodes.summary 列）。"""
        d = self.model_dump()
        d.pop("summary", None)
        d.pop("meta", None)
        return d


_LEAF_INSTRUCTION = (
    "请把本次标记为待压缩的**原始对话**压缩为结构化 JSON（只输出 JSON，不要输出其他文字）。\n"
    "**范围**：标有 {marker} 的节点是更早内容的已有摘要，**不要重新压缩、不要合并进结果**；"
    "没有该标记的普通对话消息（第 {start}-{end} 条）才是本次要压缩的内容。\n"
    "{cwd_line}"
    "**各字段按实际内容填写：某个字段没有相关内容时——整个字段可以直接省略（省略即空），"
    "不要写「无 / 暂无 / 没有相关信息」这类占位词，更不要为了填充而编造或硬凑空话："
    "字段少而实，好过看起来齐全。**\n"
    "字段说明：\n"
    "1. summary：这段时间发生了什么——事件、进展、决定与对话进程（叙述；不要写成清单）。\n"
    "2. task：目标 goal / 当前状态 status / 未完成待办 todo / 下一步 next_steps / 阻塞 blockers。\n"
    "3. decisions：本段做出的关键决定；**每项都要写 why（为什么这样定）**。\n"
    "4. lessons：errors（症状 symptom / 原因 cause / 修法 fix）+ patterns（可复用的成功做法）。\n"
    "5. context：env 环境与配置、commands 关键命令（可逐字）、artifacts 文件/符号引用、"
    "constraints 必须遵守的约束。\n"
    "6. user：用户性格/偏好/习惯（含工作偏好：常用工具、代码风格、沟通习惯）；没有就留空数组。\n"
    "**工具结果要高度压缩**：只提炼对后续有实际价值的结论/命令/报错/决定，"
    "不要转述工具返回的原文细节（原文已随节点保存）。\n\n"
    "请按以上要求给出压缩结果（JSON）："
)

_PARENT_INSTRUCTION = (
    "请把以下 {n} 个节点（{ids}）压缩合并为**上一层节点**的结构化 JSON（只输出 JSON）。\n"
    "**范围**：这些节点已去掉保护标记，是本次要合并的目标；若输入里还有其他带 "
    "{marker} 标记的节点，**不要动它们**。\n"
    "**合并规则（逐字段）**：\n"
    "- summary：把子节点叙述整合成一条阶段经过（保留主线与因果，不逐条罗列子事件）。\n"
    "- task：以**最新状态**为准（覆盖旧的）；未完成的 todo/blocker 保留，已完成的坍缩为「已完成」。\n"
    "- decisions：**累积去重**；保留仍然有效的 + 已被推翻的（注明被什么推翻）；why 不能丢。\n"
    "- lessons：**抽象**——把具体错误/修法归纳为可复用规则/模式；重复出现的重要错误保留。\n"
    "- context：并集去重，冲突取最新；**constraints 必须保留**（不随层级丢弃）。\n"
    "- user：合并归纳，不重复不矛盾。\n"
    "**没有相关信息的字段留空（也可以整个省略不写）；不要写「无 / 暂无」类占位词，"
    "不要为了填充而编造、猜测或重复已有内容。**\n\n"
    "请按以上要求给出压缩结果（JSON）："
)

# ── 英文压缩指令（评测用：LoCoMo-Refined 英文对话，session.summary_language="en"）──

_LEAF_INSTRUCTION_EN = (
    "Compress the **raw conversation** marked for compaction into structured JSON "
    "(output ONLY JSON, no other text).\n"
    "**Always write all field values in English.**\n"
    "**Scope**: entries marked {marker} are summaries of earlier content — do NOT "
    "re-compress them and do NOT merge them into the result; the unmarked raw messages "
    "(turns {start}-{end}) are what you compress.\n"
    "{cwd_line}"
    "**Fill each field from actual content: if a field has nothing relevant, omit it entirely "
    "(an omitted field counts as empty). Never write placeholders like \"none\", \"N/A\" or "
    "\"nothing to report\", and never fabricate or pad just to fill fields — fewer, real "
    "fields beat complete-looking ones.**\n"
    "Fields:\n"
    "1. summary: what happened in this span — events, progress, decisions and the flow of the conversation (a narrative, not a list).\n"
    "2. task: goal / status / todo (open items) / next_steps / blockers.\n"
    "3. decisions: key decisions made here; **always include why for each**.\n"
    "4. lessons: errors (symptom / cause / fix) + patterns (reusable things that worked).\n"
    "5. context: env (environment/config), commands (verbatim is fine), artifacts (file/symbol refs), "
    "constraints (rules that must be obeyed).\n"
    "6. user: the user's traits/preferences/habits (incl. working preferences such as tools, code style, "
    "communication habits); empty array if none.\n"
    "**Compress tool results heavily**: keep only conclusions/commands/errors/decisions of lasting value; "
    "do not paraphrase verbatim tool output (the raw text is stored with the node).\n\n"
    "Please give the compression result (JSON):"
)

_PARENT_INSTRUCTION_EN = (
    "Merge the following {n} nodes ({ids}) into ONE **parent node** as structured JSON "
    "(output ONLY JSON).\n"
    "**Always write all field values in English.**\n"
    "**Scope**: these nodes have no protection marker — they are the merge targets; if any other node "
    "in the input carries the {marker} marker, do NOT touch it.\n"
    "**Merge rules (per field)**:\n"
    "- summary: integrate the child narratives into one phase narrative (keep the through-line and causality, "
    "do not list child events one by one).\n"
    "- task: the LATEST state wins (supersede); keep unresolved todos/blockers, collapse completed ones to \"done\".\n"
    "- decisions: accumulate and dedupe; keep both still-valid and overturned decisions (note what overturned them); never drop the why.\n"
    "- lessons: abstract concrete errors/fixes into reusable rules/patterns; keep recurring important errors.\n"
    "- context: union and dedupe, newest wins on conflicts; **constraints MUST be preserved** (never dropped by level).\n"
    "- user: merge and consolidate; no duplicates or contradictions.\n"
    "**Leave fields empty when there is nothing relevant (omitting the field is fine); no "
    "placeholders like \"none\"/\"N/A\", no fabrication, guessing or repetition just to fill them.**\n\n"
    "Please give the compression result (JSON):"
)


class SummarizationMiddleware(AgentMiddleware):
    """压缩 + 记忆树一体化（任务型 schema；见 docs/代码Agent-实施计划.md 六、七节）。

    触发（后台压缩线程调用 compress_snapshot）：
      1) 原始消息数 ≥ trigger → 叶子压缩（原文 → level0 节点）
      2) 树 level L 活跃节点 ≥ threshold → 向上合并一层（递归）

    压缩输入（「未来信息」不发送：保留的尾部原始消息不进输入）：
      叶子：[SystemMessage(角色卡)] + [带保护标记的已有节点] + [待压原始消息] + [指令]
      父层：[SystemMessage(角色卡)] + [节点消息：目标去标记、其他保留] + [指令]
      —— 目标 = 输入里唯一没有 NODE_MARKER 保护的内容；指令里同时点名（双保险）。

    输出：MemoryNodeOutput（summary + task/decisions/lessons/context/user），经
    with_structured_output(json_mode) 主通道 / model_validate_json 降级解析；
    按 node_token_budget 做软裁剪（硬上限由压缩模型 max_tokens 承担）。

    落树：叶子 add_leaf（details 存原文；字段载荷存 meta.fields）/ 父节点 compact
    （子节点 is_active=0）；buffer：RemoveMessage 删被压缩消息 + 追加带保护标记的
    节点消息（metadata["node_id"]/["node_start"]，随 buffer 持久化）。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session
        # 压缩摘要语言（评测用）：session.summary_language="en" → 英文指令
        self.language: str = (getattr(session, "summary_language", None) or "zh").lower()

    def _leaf_instruction(self, start: int, end: int) -> str:
        tpl = _LEAF_INSTRUCTION_EN if self.language == "en" else _LEAF_INSTRUCTION
        cwd = self._current_cwd()
        if not cwd:
            cwd_line = ""
        elif self.language == "en":
            cwd_line = ("- Current working directory (the system fills context.cwd; "
                        f"you don't need to): {cwd}\n")
        else:
            cwd_line = f"- 当前工作目录（系统会填进 context.cwd，你不用填）：{cwd}\n"
        return tpl.format(marker=NODE_MARKER, start=start, end=end, cwd_line=cwd_line)

    def _current_cwd(self) -> str:
        """当前工作目录字符串（无工作区时为空；节点记录用，代码填写防幻觉）。"""
        try:
            cwd = self.session.effective_cwd()
        except Exception:
            cwd = None
        return str(cwd) if cwd is not None else ""

    @staticmethod
    def _merge_cwd(children: list[dict]) -> str:
        """父节点工作目录 = 子节点 context.cwd 去重合并（≤3 个，多则省略）。"""
        vals: list[str] = []
        for n in children:
            meta = n.get("meta") if isinstance(n.get("meta"), dict) else {}
            fields = meta.get("fields") or {}
            c = str((fields.get("context") or {}).get("cwd") or "").strip()
            if c and c not in vals:
                vals.append(c)
        if not vals:
            return ""
        if len(vals) <= 3:
            return "；".join(vals)
        return "；".join(vals[:3]) + "…"

    def _parent_instruction(self, ids: list[str]) -> str:
        tpl = _PARENT_INSTRUCTION_EN if self.language == "en" else _PARENT_INSTRUCTION
        return tpl.format(marker=NODE_MARKER, n=len(ids), ids="、".join(ids))

    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        # 压缩已改为异步线程（session._maybe_compress_async / _run_async_compression）：
        # 达到阈值后由后台线程对 buffer 快照压缩、空闲时原子替换，
        # 不再同步阻塞生成（压缩 LLM 调用 10~30 秒会卡住整轮回复）。
        return None

    def compress_snapshot(self, msgs: list) -> tuple[list, list]:
        """异步压缩入口：基于 buffer 快照压缩，返回 (removes, adds)。

        与同步版 _maybe_compress 同逻辑，但不修改任何 state——
        removes 为 RemoveMessage（按 id 删除），adds 为节点消息；
        由 session 压缩线程在空闲时应用到 recent_buffer。
        压缩参数从 session.settings 动态读取（用户设置，应用即生效）。
        """
        removes: list[RemoveMessage] = []
        adds: list[HumanMessage] = []

        st = getattr(self.session, "settings", None) or {}
        keep = int(st.get("compress_keep", SUMMARIZE_KEEP_MESSAGES))
        token_budget = int(st.get("compress_token_budget", SUMMARIZE_TOKEN_BUDGET))
        threshold = int(st.get("compact_threshold", LEVEL_COMPACT_THRESHOLD))
        factor = int(st.get("compact_factor", BRANCHING_FACTOR))

        raw = [m for m in msgs if "node_id" not in (getattr(m, "metadata", None) or {})]
        if should_compress_raw(raw, keep, token_budget):
            # 正常保留 keep 条；若连 keep 条都超预算（单条超大工具输出场景），
            # 只保留最后 1 条——否则永远圈不出批次（"超预算却不压缩"卡死）
            keep_n = keep if len(raw) > keep else 1
            batch = raw[:len(raw) - keep_n]
            if batch:
                # 扩展 batch：batch 末尾 ai(tool_calls) 的配套 tool 消息也纳入
                # （否则删 ai 留 tool → 孤儿 ToolMessage → DeepSeek 400）
                batch = self._extend_batch_for_tool_pairs(msgs, batch)
                self._compress_batch(msgs, batch, removes, adds)

        tree = self.session.get_memory_tree()
        level = 0
        while tree.get_level_count(level) >= threshold:
            if not self._compact_level(msgs, level, removes, adds, threshold, factor):
                break
            level += 1

        return removes, adds

    @staticmethod
    def _extend_batch_for_tool_pairs(msgs: list, batch: list) -> list:
        """把 batch 边界外、与 batch 内 tool_calls 配对的 tool 消息纳入 batch。

        batch 是"最早的 N 条"，若截断处恰好切开 ai(tool_calls) 与其 tool
        结果，压缩会删除 ai 而留下孤儿 tool 消息 → DeepSeek 400。
        对齐小B compress_buffer 的 batch_tool_call_ids 扩展逻辑。
        """
        call_ids = set()
        for m in batch:
            for tc in (getattr(m, "tool_calls", None) or []):
                call_ids.add(tc.get("id"))
        if not call_ids:
            return batch
        batch_ids = {getattr(m, "id", None) or id(m) for m in batch}
        extended = list(batch)
        for m in msgs:
            if (getattr(m, "id", None) or id(m)) in batch_ids:
                continue
            if (getattr(m, "type", None) == "tool"
                    and getattr(m, "tool_call_id", None) in call_ids):
                extended.append(m)
        return extended

    def _compress_batch(self, msgs, batch, removes, adds) -> None:
        """压缩一个批次 → 节点（**默认整批一次调用 = 一个节点**）。

        只有两种例外才回退分片（每片 ≤3 万 token，逐片各落一个节点）：
        ① 批次估算超过 `_ONE_SHOT_MAX_TOKENS`（超大积压，防单次调用超窗）；
        ② 整批调用失败（如端点上下文超限）——失败不落任何东西，分片重试。
        （分片最初是无条件启用的，用户反馈"一次压缩冒出好几个 L0 节点、感觉
        不是 15 万压一次"后改为"整批优先"——2026-10-18。）
        """
        est = self._estimate_messages_tokens(batch)
        if est <= _ONE_SHOT_MAX_TOKENS:
            if self._summarize_leaf(msgs, batch, removes, adds):
                return
            _logger.warning("[compress] 整批压缩失败，回退分片重试（估算 %d token）", est)
        chunks = self._split_batch_by_tokens(batch)
        if len(chunks) <= 1:
            return   # 只有一片（且已失败）：本轮放弃，下次触发再试
        for chunk in chunks:
            if not self._summarize_leaf(msgs, chunk, removes, adds):
                break   # 本片失败：停手（后续片的全局序号会错位）

    @staticmethod
    def _estimate_message_tokens(m) -> int:
        """单条消息的估算 token（字符 × 1.2，与压缩触发同口径）。"""
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            text = "".join(str(b.get("text") or "") for b in content
                           if isinstance(b, dict))
        else:
            text = str(content)
        return int(len(text) * 1.2)

    @classmethod
    def _estimate_messages_tokens(cls, msgs: list) -> int:
        return sum(cls._estimate_message_tokens(m) for m in msgs)

    @staticmethod
    def _split_batch_by_tokens(batch: list, max_tokens: int = 30000,
                               max_chunks: int = 12) -> list[list]:
        """按估算 token 把叶子批次切成分片（每次摘要调用输入别超 ~3 万 token）。

        只在**安全边界**切：下一段不能以 tool 消息开头、本段不能以带 tool_calls
        的 ai 结尾——批次是作为消息发给模型的，tool 配对断裂会直接 400。
        一次最多切 max_chunks 片（剩下的留给下一轮压缩），避免超大积压把一轮
        压缩拖成几十分钟。**注意：分片是兜底路径**（整批优先，见 _compress_batch）。
        """
        est = SummarizationMiddleware._estimate_message_tokens

        chunks: list[list] = []
        cur: list = []
        cur_tokens = 0
        for i, m in enumerate(batch):
            cur.append(m)
            cur_tokens += est(m)
            if cur_tokens < max_tokens or i == len(batch) - 1:
                continue
            nxt = batch[i + 1]
            if (getattr(nxt, "type", None) == "tool"
                    or getattr(m, "tool_calls", None)):
                continue   # 不安全边界：继续累积到配对完整再切
            chunks.append(cur)
            cur = []
            cur_tokens = 0
            if len(chunks) >= max_chunks:
                break
        if cur:
            chunks.append(cur)
        return chunks

    def _maybe_compress(self, state: PlannerState) -> dict | None:
        """保留：与 compress_snapshot 等价（兼容旧调用/测试），before_model 不再调用。"""
        removes, adds = self.compress_snapshot(list(state.get("messages") or []))
        if removes or adds:
            return {"messages": removes + adds}
        return None

    # ── 节点消息渲染（全字段，供模型上下文与 explore 使用）──────

    @staticmethod
    def _fmt_time_range(start: str, end: str) -> str:
        """节点起止时间 → 'YYYY-MM-DD HH:MM ~ HH:MM'（同日省略结束日期，
        跨日/跨年结束时间带完整日期，避免歧义）。"""
        if not start or not end:
            return ""
        full = lambda ts: ts[:16]      # YYYY-MM-DD HH:MM
        hm = lambda ts: ts[11:16]      # HH:MM
        if start[:10] == end[:10]:
            return f"{full(start)} ~ {hm(end)}"
        return f"{full(start)} ~ {full(end)}"

    @staticmethod
    def _node_level(node_id: str) -> int:
        """从节点 id 解析层级：node{level}_{seq} → level。"""
        try:
            return int(str(node_id).split("_", 1)[0][4:])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _render_fields_lines(fields: dict) -> list[str]:
        """任务型字段载荷（dict）→ 文本行（空字段省略）。"""
        lines: list[str] = []
        t = fields.get("task") or {}
        parts = []
        if t.get("goal"):
            parts.append(f"目标：{t['goal']}")
        if t.get("status"):
            parts.append(f"状态：{t['status']}")
        if t.get("todo"):
            parts.append("待办：" + "、".join(t["todo"]))
        if t.get("next_steps"):
            parts.append("下一步：" + "、".join(t["next_steps"]))
        if t.get("blockers"):
            parts.append("阻塞：" + "、".join(t["blockers"]))
        if parts:
            lines.append("[任务] " + "；".join(parts))
        decisions = fields.get("decisions") or []
        items = []
        for d in decisions:
            what, why = d.get("what") or "", d.get("why") or ""
            if what:
                items.append(f"{what}（因为 {why}）" if why else what)
        if items:
            lines.append("[决策] " + "；".join(items))
        lessons = fields.get("lessons") or {}
        parts = []
        for e in (lessons.get("errors") or []):
            seg = f"{e.get('symptom', '')}→{e.get('fix', '')}".strip("→")
            if e.get("cause"):
                seg += f"（原因：{e['cause']}）"
            if seg:
                parts.append(seg)
        parts.extend(str(x) for x in (lessons.get("patterns") or []) if x)
        if parts:
            lines.append("[经验] " + "；".join(parts))
        c = fields.get("context") or {}
        parts = []
        if c.get("cwd"):
            parts.append(f"目录：{c['cwd']}")
        if c.get("env"):
            parts.append("环境：" + "、".join(c["env"]))
        if c.get("commands"):
            parts.append("命令：" + "、".join(c["commands"]))
        if c.get("artifacts"):
            parts.append("引用：" + "、".join(c["artifacts"]))
        if c.get("constraints"):
            parts.append("约束：" + "、".join(c["constraints"]))
        if parts:
            lines.append("[环境] " + "；".join(parts))
        user = fields.get("user") or []
        if user:
            lines.append("[用户] " + "、".join(user))
        return lines

    @staticmethod
    def _render_node_text(node_id: str, start, end, out: MemoryNodeOutput,
                          time_range: tuple[str, str] | None = None,
                          marker: bool = True) -> str:
        """节点消息文本（进对话上下文）。

        marker=True：带「已压缩 · 不需要压缩」保护标记（常驻上下文）；
        marker=False：去掉标记（节点压缩时目标节点的输入版本）。
        """
        lvl = SummarizationMiddleware._node_level(node_id)
        head = f"[{node_id} · L{lvl} · 第{start}-{end}条]"
        if marker:
            head += f" {NODE_MARKER}"
        lines = [head, f"[摘要] {clean_wrapped_summary(out.summary)}"]
        if time_range and time_range[0] and time_range[1]:
            lines.append(f"[时间] {SummarizationMiddleware._fmt_time_range(*time_range)}")
        lines.extend(SummarizationMiddleware._render_fields_lines(out.fields_payload()))
        return "\n".join(lines)

    @staticmethod
    def _node_full_text(n: dict) -> str:
        """树节点 dict（get_nodes_at_level 返回）→ 全字段文本（无保护标记）。

        新节点渲染 meta.fields（任务型）；旧节点兼容渲染 profile/future_notes（画像型）。
        """
        rr = n.get("round_range") or ["?", "?"]
        nid = n.get("id") or "?"
        lvl = n.get("level")
        if lvl is None:
            lvl = SummarizationMiddleware._node_level(nid)
        lines = [f"[{nid} · L{lvl} · 第{rr[0]}-{rr[1]}条]",
                 f"[摘要] {clean_wrapped_summary(str(n.get('summary', '')))}"]
        tr = SummarizationMiddleware._fmt_time_range(
            n.get("time_start") or "", n.get("time_end") or "")
        if tr:
            lines.append(f"[时间] {tr}")
        meta = n.get("meta") if isinstance(n.get("meta"), dict) else {}
        fields = meta.get("fields")
        if isinstance(fields, dict):
            lines.extend(SummarizationMiddleware._render_fields_lines(fields))
        else:
            # 兼容旧节点（schema v1：画像四维度 / future_notes）
            p = n.get("profile") or {}
            for label, key in (("喜好", "preferences"), ("性格", "personality"),
                               ("习惯", "habits"), ("目标", "goals")):
                items = p.get(key) or []
                if items:
                    lines.append(f"[{label}] " + "、".join(items))
            details = n.get("details")
            if isinstance(details, dict) and details.get("future_notes"):
                lines.append("[后续说明] " + "；".join(details["future_notes"]))
        return "\n".join(lines)

    # ── 叶子压缩（对话 → level0 节点）────────────────────────

    def _summarize_leaf(self, msgs, batch, removes, adds) -> bool:
        """压缩一个叶子分片 → level0 节点；成功返回 True（失败时调用方停手）。"""
        tree = self.session.get_memory_tree()
        # 全局序号（对齐小B _span 机制）：节点范围 = 已压缩累计条数起，
        # 连续不重叠——不能用 enumerate(msgs) 相对索引（每次从 0 附近开始，
        # 多次压缩的节点范围看起来全是从"第0条"开始，假重叠）
        start_pos = self.session._compressed_total
        end_pos = start_pos + len(batch) - 1

        # 压缩输入边界（计划 7.2）：保留的尾部原始消息不进输入（"未来信息"不发送）；
        # 已有节点带保护标记作为上下文（模型据此知道"这些不要动"）
        node_msgs = [m for m in msgs
                     if (getattr(m, "metadata", None) or {}).get("node_id")]
        instruction = HumanMessage(content=self._leaf_instruction(start_pos, end_pos))
        out = self._call_compress(node_msgs + list(batch), instruction)
        if not out or not out.summary or not out.summary.strip():
            _logger.warning("[compress] 叶子摘要为空，跳过")
            return False
        out.context.cwd = self._current_cwd()[:300]   # 工作目录由代码填写（防幻觉）

        from langchain_core.messages import messages_to_dict
        # 覆盖时间范围：batch 首/末消息的 metadata.ts（秒级 ISO，UTC+8）
        def _msg_ts(m):
            return (getattr(m, "metadata", None) or {}).get("ts")
        first_ts = _msg_ts(batch[0])
        last_ts = _msg_ts(batch[-1])
        time_range = (first_ts, last_ts) if (first_ts and last_ts) else None
        # 清洗 details：image_url 块对模型无用（tool 消息不能带图），base64 落盘
        # 纯浪费存储，探索记忆时还会被当垃圾文本灌回上下文——序列化后替换为文本占位。
        # 兼容两种 messages_to_dict 输出形状：旧版 {"type","content"}，新版 {"type","data":{...}}
        details = messages_to_dict(batch)
        for d in details:
            content = d.get("content")
            if content is None and isinstance(d.get("data"), dict):
                content = d["data"].get("content")
            if isinstance(content, list):
                kept = [b for b in content
                        if not (isinstance(b, dict) and b.get("type") == "image_url")]
                cleaned = kept if kept else [{"type": "text", "text": "（图片）"}]
                if "content" in d:
                    d["content"] = cleaned
                else:
                    d["data"]["content"] = cleaned
        meta = dict(out.meta)
        meta["schema_version"] = SCHEMA_VERSION
        meta["fields"] = out.fields_payload()
        node_id = tree.add_leaf(
            out.summary,
            (start_pos, end_pos),
            None,
            details=details,
            profile=None,
            meta=meta,
            time_range=time_range,
        )
        self.session._compressed_total = end_pos + 1   # 累加已压缩条数
        node_text = self._render_node_text(node_id, start_pos, end_pos, out, time_range)
        # node_start：全局起始序号——回写 buffer 后据此把节点移到
        # 正确位置（压缩的总是最早的消息，节点应排在 buffer 最前）
        summary_msg = HumanMessage(
            content=node_text,
            metadata={"node_id": node_id, "node_start": start_pos})
        removes.extend(RemoveMessage(id=m.id) for m in batch)
        adds.append(summary_msg)
        _logger.info("[compress] 叶子 %s 第%d-%d条（%d 条消息落树，字段=%s）",
                     node_id, start_pos, end_pos, len(batch),
                     "、".join(k for k, v in out.fields_payload().items() if v))
        return True

    # ── 向上压缩（递归：摘要还能再被摘要）────────────────────

    def _compact_level(self, msgs, level, removes, adds,
                       threshold: int | None = None,
                       factor: int | None = None) -> bool:
        tree = self.session.get_memory_tree()
        if threshold is None:
            st = getattr(self.session, "settings", None) or {}
            threshold = int(st.get("compact_threshold", LEVEL_COMPACT_THRESHOLD))
            factor = int(st.get("compact_factor", BRANCHING_FACTOR))
        nodes = tree.get_nodes_at_level(level)
        if len(nodes) < threshold:
            return False
        to_compact = nodes[:factor]

        by_id = {}
        for m in msgs:
            nid = (getattr(m, "metadata", None) or {}).get("node_id")
            if nid:
                by_id[nid] = m
        target_ids = {n["id"] for n in to_compact}
        # 压缩输入边界（计划 7.2）：只放节点消息（原始消息不进）；目标节点从树渲染
        # → 去掉保护标记（模型据此知道"这几条要压"）；其他节点保留标记 = 别动
        context_msgs = []
        removable = []
        seen_ids = set()
        for m in msgs:
            nid = (getattr(m, "metadata", None) or {}).get("node_id")
            if not nid:
                continue
            seen_ids.add(nid)
            if nid in target_ids:
                n = next((x for x in to_compact if x["id"] == nid), None)
                text = self._node_full_text(n) if n else (m.content or "")
                context_msgs.append(HumanMessage(content=text, metadata={"node_id": nid}))
                removable.append(m)
            else:
                context_msgs.append(m)          # 其他节点：原样（带保护标记）
        for n in to_compact:                    # 目标不在 buffer（异常）→ 补上
            if n["id"] not in seen_ids:
                context_msgs.append(HumanMessage(content=self._node_full_text(n),
                                                 metadata={"node_id": n["id"]}))

        instruction = HumanMessage(
            content=self._parent_instruction([n["id"] for n in to_compact]))
        out = self._call_compress(context_msgs, instruction)
        if not out or not out.summary or not out.summary.strip():
            _logger.warning("[compress] 父节点摘要为空，停止向上压缩")
            return False
        merged_cwd = self._merge_cwd(to_compact)
        if merged_cwd:
            out.context.cwd = merged_cwd[:300]        # 工作目录从子节点合并

        meta = dict(out.meta)
        meta["schema_version"] = SCHEMA_VERSION
        meta["fields"] = out.fields_payload()
        parent_id = tree.compact(
            [n["id"] for n in to_compact], out.summary,
            profile=None,
            meta=meta)
        rr0 = to_compact[0].get("round_range") or ["?", "?"]
        rr1 = to_compact[-1].get("round_range") or ["?", "?"]
        # 父节点 node_start：子节点覆盖区间的最小起点（优先取 buffer 节点消息
        # 的 node_start，缺失时取树的 round_range 起始轮次数值化）
        child_starts = []
        for n in to_compact:
            m = by_id.get(n["id"])
            if m is not None:
                cs = (getattr(m, "metadata", None) or {}).get("node_start")
                if cs is not None:
                    child_starts.append(int(cs))
            try:
                child_starts.append(int(n.get("round_range")[0]))
            except (TypeError, ValueError, IndexError):
                pass
        node_start = min(child_starts) if child_starts else 0
        # 父节点渲染必须带时间范围（自身优先，历史数据从子节点聚合）——
        # 否则模型在 buffer 里看不到 [时间]，回忆时无从按时间定位
        parent_text = self._render_node_text(
            parent_id, rr0[0], rr1[1], out,
            time_range=tree.get_effective_time_range(parent_id),
        )
        parent_msg = HumanMessage(
            content=parent_text,
            metadata={"node_id": parent_id, "node_start": node_start},
        )
        removes.extend(RemoveMessage(id=m.id) for m in removable)
        adds.append(parent_msg)
        _logger.info("[compress] 父节点 %s（%d 个 level%d 节点合并）",
                     parent_id, len(to_compact), level)
        return True

    @staticmethod
    def _apply_budget(out: MemoryNodeOutput, budget: int) -> MemoryNodeOutput:
        """节点预算软裁剪（硬上限由压缩模型 max_tokens 承担）。

        弱模型偶尔写超长；这里对 summary 与各列表做兜底截断，避免单节点膨胀。
        """
        cap = max(200, int(budget or 1000))
        if len(out.summary) > cap * 2:
            out.summary = out.summary[: cap * 2].rstrip() + "…"

        def _clip(items, n: int = 8, width: int = 200) -> list[str]:
            return [str(x)[:width] for x in list(items or [])[:n] if x]

        t = out.task
        t.goal, t.status = t.goal[:300], t.status[:300]
        t.todo, t.next_steps = _clip(t.todo), _clip(t.next_steps)
        t.blockers = _clip(t.blockers)
        out.decisions = [DecisionInfo(what=d.what[:200], why=d.why[:300])
                         for d in out.decisions[:8]]
        out.lessons.errors = [LessonError(symptom=e.symptom[:200], cause=e.cause[:200],
                                          fix=e.fix[:300])
                              for e in out.lessons.errors[:8]]
        out.lessons.patterns = _clip(out.lessons.patterns)
        c = out.context
        c.cwd = str(c.cwd or "")[:300]
        c.env, c.commands = _clip(c.env), _clip(c.commands)
        c.artifacts, c.constraints = _clip(c.artifacts), _clip(c.constraints)
        out.user = _clip(out.user, n=12)
        return out

    # ── 压缩请求 ────────────────────────────────────────────

    def _call_compress(self, context_msgs, instruction) -> MemoryNodeOutput | None:
        """压缩调用入口：解析结果后按 node_token_budget 做软裁剪。"""
        out = self._invoke_compress(context_msgs, instruction)
        if out is None:
            return None
        budget = int((getattr(self.session, "settings", None) or {})
                     .get("node_token_budget", 1000) or 1000)
        return self._apply_budget(out, budget)

    def _invoke_compress(self, context_msgs, instruction) -> MemoryNodeOutput | None:
        """独立压缩模型调用：system(角色卡) + 上下文 + 指令。

        主通道：with_structured_output(json_mode)——JSON Schema 保证输出可解析；
        降级 1：文本 JSON model_validate_json；降级 2：纯摘要兜底。
        某些端点不支持 structured/json_mode（实测 OpenCode Go 每次都失败）：
        首次失败后在 session 上打标，本进程内直接走降级通道——省掉每次注定
        失败的结构化调用（每次要等 ~15s）。
        """
        from langchain_core.messages import SystemMessage
        model = self.session._get_summary_model()
        # 评测/多语言场景可覆盖压缩用 system prompt（默认 = 主会话角色卡）：
        # 中文角色卡会压过英文压缩指令，导致摘要语言不受控
        sys_prompt = (getattr(self.session, "summary_system_prompt", None)
                      or self.session.system_prompt)
        request = [SystemMessage(content=sys_prompt)]
        request.extend(context_msgs)
        request.append(instruction)
        if not getattr(self.session, "_compress_no_structured", False):
            try:
                structured = model.with_structured_output(MemoryNodeOutput, method="json_mode")
                out = structured.invoke(request)
                if isinstance(out, MemoryNodeOutput):
                    return out
                if isinstance(out, dict):
                    return MemoryNodeOutput.model_validate(out)
            except Exception as exc:
                self.session._compress_no_structured = True   # 端点不支持：别再试
                _logger.warning("[compress] with_structured_output 失败，"
                                "后续直接降级解析：%s", exc)
        try:
            r = model.invoke(request)
            text = _strip_json_fence((r.content or "").strip())
            return MemoryNodeOutput.model_validate_json(text)
        except Exception as exc:
            _logger.warning("[compress] JSON 解析失败，重试一次：%s", exc)
            try:
                r = model.invoke(request)
                text = _strip_json_fence((r.content or "").strip())
            except Exception as exc2:
                _logger.warning("[compress] 压缩调用失败：%s", exc2)
                return None
            # 第二次也先按 JSON 解析（模型输出有随机性，这次可能完整——
            # 曾漏了这步：整段 JSON 被当纯摘要存进节点，2026-10-18 事故）
            try:
                return MemoryNodeOutput.model_validate_json(text)
            except Exception:
                pass
            # 截断/坏 JSON → 抠出 summary 字符串（远好于把 JSON 整段存成摘要）
            cleaned = clean_wrapped_summary(text)
            if cleaned and cleaned != text:
                _logger.warning("[compress] JSON 不完整，已抠出 summary 存为纯摘要")
                return MemoryNodeOutput(summary=cleaned)
            if not text:
                _logger.warning("[compress] 纯摘要调用返回空内容")
                return None
            return MemoryNodeOutput(summary=text)

def _strip_json_fence(text: str) -> str:
    """剥掉模型输出的 markdown ```json 代码块围栏（降级解析前清洗）。"""
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    return s


def clean_wrapped_summary(text: str) -> str:
    """把"JSON 外壳"摘要剥成纯文本（2026-10-18 降级路径事故的兜底）。

    事故背景：max_tokens 截断导致 JSON 解析失败时，降级路径把整段 JSON 原样
    存成了摘要（节点/节点消息里全是 `{"summary": ...}`）。这里：
    - 完整 JSON → json.loads 取 summary 字段；
    - 被截断的 JSON → 正则抠出 summary 字符串；
    - 本来就不是 JSON → 原样返回。
    用于渲染层（节点消息/树渲染）与压缩降级路径，历史坏数据在展示时自动剥壳。
    """
    import re as _re

    s = str(text or "").strip()
    if not s.startswith("{"):
        return text
    try:
        d = json.loads(s)
        if isinstance(d, dict) and isinstance(d.get("summary"), str) and d["summary"].strip():
            return d["summary"].strip()
    except Exception:
        pass
    m = _re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)', s)
    if m:
        raw = m.group(1)
        try:
            val = json.loads('"' + raw + '"')   # 正常闭合时按 JSON 反转义
        except Exception:
            # 断在字符串中间（无闭合引号）：做基本反转义
            val = raw.replace("\\n", "\n").replace('\\"', '"')
        if str(val).strip():
            return str(val).strip()
    return text
