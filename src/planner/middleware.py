"""planner 业务中间件（LangChain AgentMiddleware，移植自 yaya backend）。

钩子语义：
- node-style（before_agent / before_model / after_model）：顺序执行，返回 dict 合并进
  state（走 reducer），支持 {"jump_to": "end"} 提前跳转；
- wrap-style（wrap_model_call / wrap_tool_call）：包裹每次调用，可短路/重试/转换。

中间件链（agent.py 中按序）：
- PlayerPriorityMiddleware before_model   玩家消息在 _inbox → 跳 end（让位）
- HeartbeatTrackMiddleware wrap_tool_call 记录 + before_model 跳转（heartbeat 即停）
- ApprovalMiddleware      wrap_tool_call 代码工作区审批（写/区外读挂起，黑名单硬拒）
- LoggingMiddleware       wrap_*        每轮 LLM/工具调用写 jsonl 日志

ReAct 循环：**用 create_agent 内建的原生循环**（model → tools → model → …），
模型不再返回工具调用就自然结束本轮。中间件只负责"提前跳出"的几种情况
（玩家让位 / heartbeat / 用户停止），**不主动截断工具→模型的回路**。
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

from . import prompt_loader
from .agent_context import current_agent
from .content import content_text
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

    set_heartbeat_called: NotRequired[bool]
    plan_injected: NotRequired[bool]


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
        default_text = prompt_loader.load("runtime/screenshot")
        for entry in pending:
            if isinstance(entry, dict):
                url = str(entry.get("url") or "")
                text = str(entry.get("text") or default_text)
                kwargs: dict = {}
                if entry.get("viewed"):
                    kwargs["viewed_image"] = entry["viewed"]
            else:   # 兼容纯 URL 字符串（旧格式/外部注入）
                url, text, kwargs = str(entry), default_text, {}
            if not url:
                continue
            msgs.append(HumanMessage(
                content=[
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
                additional_kwargs=kwargs,
                metadata={"injected": True, "kind": "image"}))
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


# ── heartbeat 即停止 ─────────────────────────────────────────

class HeartbeatTrackMiddleware(AgentMiddleware):
    """wrap_tool_call 记录 heartbeat 调用；before_model 调过 → jump_to:"end"。

    关键语义：create_agent 的循环在工具执行后总会再调模型（停止条件是"模型不再
    返回工具调用"），必须在 before_model 跳转——若用 after_model，heartbeat 标记
    会在下一次模型调用后立即跳转，导致该轮其他工具
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
        # White 子代理：没有命令权限；文件操作只能在主工作区内（硬拒绝，不走审批）
        if current_agent() == "white":
            if name in RUN_TOOLS:
                return "deny", "（White 不能执行命令。）"
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
        # White：文件读写硬限制在工作区内（工作区未启用则完全禁文件）
        if current_agent() == "white":
            if workspace is None:
                return "deny", "（White 未启用代码工作区，不能读写文件。）"
            for p in targets:
                if not is_within(workspace, p):
                    return "deny", f"（White 只允许在工作区内操作，已拒绝：{p}）"
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
        grant_db = self.session.get_permission_db()
        pending: list[tuple[str, str]] = []       # [(tool, 工作区相对路径)] 待审批
        for p in targets:
            inside = is_within(workspace, p)
            rel = rel_posix(workspace, p)
            if inside and name in READ_TOOLS:
                continue                              # 区内只读 → 放行
            if name in WRITE_TOOLS and not inside:
                return "deny", (
                    f"（写权限只允许当前工作区内的路径，已取消：{p}。"
                    "工作区外只能申请只读授权。）")
            if not inside and name in READ_TOOLS and grant_db.has_read(p):
                continue                              # 已授权的递归读目录
            if (not inside and name in READ_TOOLS
                    and not st.get("code_read_outside_workspace", False)):
                return "deny", (
                    f"（工作区外的读取未授权，已取消：{p}。"
                    "可在「权限」页添加只读目录，或调用 request_read_permission 申请。）")
            if (mode == "auto" and name in WRITE_TOOLS and inside
                    and cwd is not None and is_within(cwd, p)):
                continue                              # Auto：当前目录内写自动放行
            # 区外读只认 SQLite 目录授权表；不再用旧的 permissions.json 规则，
            # 否则在「权限」页移除目录后，旧规则仍会悄悄放行。
            if (inside or name not in READ_TOOLS) and store.match(name, rel):
                continue                              # 命中 always 规则
            pending.append((name, rel))
        if not pending:
            return "allow", ""
        preview = self._approval_preview(name, args)
        outside_reads = [
            Path(rel) for tool, rel in pending
            if tool in READ_TOOLS and not is_within(workspace, Path(rel))
        ]
        scopes = (self._read_approval_scopes(outside_reads)
                  if outside_reads and len(outside_reads) == len(pending)
                  else self._approval_scopes(pending))
        decision = self.session.request_approval(
            name, [str(p) for p in targets],
            preview=preview,
            scopes=scopes,
            cwd=str(cwd) if cwd is not None else "",
        )
        if decision.startswith("always"):
            granted = False
            for tool, rel in pending:
                target = Path(rel)
                outside_read = (tool in READ_TOOLS and not is_within(workspace, target))
                if outside_read:
                    # 区外读由目录授权页统一管理；always_dir 落 SQLite，
                    # 其他 always_* 不写旧 JSON 规则（避免页面移除后仍放行）。
                    if decision == "always_dir":
                        target_dir = target if target.is_dir() else target.parent
                        r = grant_db.add_read(target_dir, source="approval")
                        granted = granted or bool(r.get("ok"))
                    continue
                store.add(tool, self._scope_glob(rel, decision))
            if granted:
                self.session.push_event({"type": "permissions_update"})
            return "allow", ""
        if decision == "allow_once":
            return "allow", ""
        if decision == "timeout":
            return "deny", "（审批超时，已自动取消该操作。）"
        if decision == "stop":
            return "deny", "（用户中断了本次生成，操作已取消。）"
        return "deny", "（用户拒绝了此操作，已取消。）"

    @staticmethod
    def _read_approval_scopes(paths: list[Path]) -> list[dict]:
        """工作区外读的授权范围：目录优先（递归），其次仅此文件。"""
        p = paths[0]
        d = p if p.is_dir() else p.parent
        return [{"kind": "dir", "label": f"此目录（{d}）", "glob": str(d)}]

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
#: 压缩分片目标大小（token）：批次超过它 → 切分片逐片压缩（设置页
#: compress_chunk_tokens 可调；0 = 关闭分片、整批一次）。
CHUNK_TOKEN_TARGET = 30000
#: 分片大小浮动窗口（为对齐"用户消息边界"允许的大小范围）
_CHUNK_FLOAT_LO = 0.75
_CHUNK_FLOAT_HI = 1.4
#: 一轮压缩里层级合并的批次上限（防超大积压把一轮拖成几十分钟）；
#: **不设置最大层数**——树可以持续向上合并，直到没有超过阈值的层级。
_MAX_COMPACT_BATCHES_PER_ROUND = 8


def should_compress_raw(raw_msgs: list, keep: int, token_budget: int) -> bool:
    """是否需要叶子压缩：**只看原始消息估算 token ≥ 预算**（条数不参与判定）。

    token 估算与 session.context_stats 同口径：文本 chars × 1.2（中文为主，偏保守），
    **图片块每张按 384 计**——不能把视觉注入的 base64 data URL 按字符数算（一张 1MB
    图 = 百万级"字符"，估算虚高 → 远低于预算就触发压缩；用户实测：预算 20 万、
    原始才 1 万多就开始压缩）。
    至少 2 条原始消息才可能圈出批次；正常保留 keep 条，连 keep 条都超预算时
    只保留最后 1 条（见 compress_snapshot 的 keep_n 兜底）。
    """
    if len(raw_msgs) <= 1:
        return False
    if token_budget <= 0:
        return False
    chars = 0
    images = 0
    for m in raw_msgs:
        content = getattr(m, "content", "") or ""
        chars += len(content_text(content))
        if isinstance(content, list):
            images += sum(1 for b in content
                          if isinstance(b, dict) and b.get("type") == "image_url")
    return int(chars * 1.2) + images * 384 >= token_budget

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

    def _leaf_instruction(self, start: int, end: int, anchor: str = "",
                          anchor_end: str = "") -> str:
        """叶子压缩指令（文本在 prompts/compress/）；anchor 非空 = 分片锚点模式。"""
        lang = "en" if self.language == "en" else "zh"
        if anchor:
            scope_name = f"compress/leaf_scope_chunk_{lang}"
        else:
            scope_name = f"compress/leaf_scope_{lang}"
        scope = prompt_loader.render(
            scope_name, marker=NODE_MARKER, start=start, end=end,
            anchor=anchor, anchor_end=anchor_end)
        cwd = self._current_cwd()
        if not cwd:
            cwd_line = ""
        elif self.language == "en":
            cwd_line = ("- Current working directory (the system fills context.cwd; "
                        f"you don't need to): {cwd}\n")
        else:
            cwd_line = f"- 当前工作目录（系统会填进 context.cwd，你不用填）：{cwd}\n"
        return prompt_loader.render(
            f"compress/leaf_{lang}", scope=scope, cwd_line=cwd_line,
            budget_line=self._budget_line()) + prompt_loader.load(
                f"compress/skeleton_{lang}")

    def _budget_line(self) -> str:
        """把 node_token_budget 写成模型可执行的**整节点总长度**预算。

        口径（2026-10-19 用户确认）：设置值 N ≈ 节点总字数 N 字（中文）——
        以前预算只影响 `max_tokens` 与事后裁剪，模型不知道，往 3 倍以上写；
        第一版按 1 token≈1.6 字折算（1000→1600 字）用户仍嫌长，改为 1:1。
        英文（评测用）保持 token 语义：1 token≈3.5 字符。
        """
        try:
            budget = int((getattr(self.session, "settings", None) or {})
                         .get("node_token_budget", 1000) or 1000)
        except (TypeError, ValueError):
            budget = 1000
        budget = max(200, budget)
        lang = "en" if self.language == "en" else "zh"
        chars = int(budget * 3.5) if self.language == "en" else budget
        return prompt_loader.render(
            f"compress/budget_{lang}", chars=chars,
            summary=chars * 2 // 5, rest=chars * 3 // 5)

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
        lang = "en" if self.language == "en" else "zh"
        base = prompt_loader.render(
            f"compress/parent_{lang}", marker=NODE_MARKER, n=len(ids),
            ids="、".join(ids), budget_line=self._budget_line())
        return base + prompt_loader.load(f"compress/skeleton_{lang}")

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

        # 层级合并：某层活跃节点数 ≥ 阈值 → 把最早的 factor 个合并成 1 个父节点。
        # **持续合并到低于阈值**（旧行为每轮每层只合一批：10 个节点(6/3) 只合 3 个、
        # 剩 7 个仍超阈值挂到下一轮——用户指出）；每轮批次有上限防积压跑飞。
        tree = self.session.get_memory_tree()
        level = 0
        batches = 0
        # 没有硬编码最大层数：先取当前最高活跃层；每合并出一层父节点，
        # max_level 随之更新，直到所有层都低于阈值（或本轮批次用完）。
        max_level = tree.get_max_level()
        while batches < _MAX_COMPACT_BATCHES_PER_ROUND:
            while level <= max_level and tree.get_level_count(level) < threshold:
                level += 1
            if level > max_level:
                break
            if not self._compact_level(msgs, level, removes, adds, threshold, factor):
                break
            batches += 1
            max_level = max(max_level, tree.get_max_level())

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
        """压缩一个批次 → 节点（**分片，2026-10-19 定稿**）。

        - 批次估算 ≤ 分片大小（`compress_chunk_tokens`，默认 3 万 token）或分片
          关闭（设 0）：一次调用 = 一个 L0 叶子（小批次不分片）；
        - 更大：切成 n 片（切点优先用户消息边界、允许大小浮动），第 k 次调用带
          "第 1..k 片原文"（**渐进前缀**——逐字节复刻聊天序列的前缀 → 缓存全
          命中），指令头/尾锚点只压最后一片；**n 个分片叶子各自进上下文**，
          之后由层级合并（阈值/合批，一轮持续合并到低于阈值）统一消化。
        - **分片按时间正序压缩，勿改成倒序**（2026-10-19 推演过）：正序时每次
          调用 = 上一次的前缀 + 新一片 → 上一次的缓存恰好覆盖公共前缀（全命中）；
          且**首片调用最小**——聊天缓存过期/被驱逐时最坏只全价一片（~3 万）。
          倒序则首调用 = 整批（15 万），且"滚动窗口式小缓存"最先驱逐开头、
          前缀匹配又必须从开头对上 → 最坏全价一整批。
        例外：某片失败 → 停手（已成功片照常进上下文）。
        """
        st = getattr(self.session, "settings", None) or {}
        chunk_tokens = int(st.get("compress_chunk_tokens", CHUNK_TOKEN_TARGET) or 0)
        est = self._estimate_messages_tokens(batch)
        if chunk_tokens <= 0 or est <= chunk_tokens:
            self._summarize_leaf(msgs, batch, removes, adds)
            return
        chunks = self._split_batch_by_tokens(batch, max_tokens=chunk_tokens)
        if len(chunks) <= 1:
            self._summarize_leaf(msgs, batch, removes, adds)
            return
        prefix: list = []
        for chunk in chunks:
            prefix.extend(chunk)
            head, tail = self._anchor_quotes(chunk)
            nid = self._summarize_leaf(
                msgs, chunk, removes, adds,
                context_msgs=list(prefix),
                anchor_quote=head, anchor_end_quote=tail)
            if not nid:
                break   # 本片失败：停手（已成功片保留）

    @staticmethod
    def _estimate_message_tokens(m) -> int:
        """单条消息的估算 token（文本 chars × 1.2，与压缩触发同口径；图片不计）。"""
        return int(len(content_text(getattr(m, "content", "") or "")) * 1.2)

    @classmethod
    def _estimate_messages_tokens(cls, msgs: list) -> int:
        return sum(cls._estimate_message_tokens(m) for m in msgs)

    @staticmethod
    def _strip_image_blocks(msgs: list) -> list:
        """压缩调用输入：image_url 块换成文本占位（摘要用不到原图）。

        base64 data URL 动辄几十万字符——发进压缩调用纯浪费（token/延迟），
        对摘要也没有价值（详情存储的同类清洗见 _summarize_leaf）。
        """
        out = []
        for m in msgs:
            content = getattr(m, "content", None)
            if not isinstance(content, list):
                out.append(m)
                continue
            kept = [b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "image_url")]
            kept = kept or [{"type": "text", "text": "（图片）"}]
            try:
                out.append(m.model_copy(update={"content": kept}))
            except Exception:
                out.append(m)
        return out

    @staticmethod
    def _split_batch_by_tokens(batch: list, max_tokens: int = 30000,
                               max_chunks: int = 12) -> list[list]:
        """按估算 token 把叶子批次切成分片（目标 ~max_tokens/片，允许浮动）。

        切点策略（2026-10-19 用户定，服务于"渐进前缀"压缩）：
        - 优先**用户消息边界**——每片 = 完整若干轮对话，摘要不会在"用户问了、
          答案在下片"处割裂；在 [0.75×, 1.4×] 的浮动窗口内遇到用户消息即收片；
        - 浮动窗口内没有用户消息边界（如一大段连续工具循环）→ 到 1.4× 后在
          第一个**安全边界**收（不切开 tool 配对：下一条不是 tool、本条不是
          带 tool_calls 的 ai——批次作为消息发给模型，配对断裂会直接 400）；
        - 一次最多切 max_chunks 片（剩下的留给下一轮压缩）。
        """
        est = SummarizationMiddleware._estimate_message_tokens

        chunks: list[list] = []
        cur: list = []
        cur_tokens = 0
        for i, m in enumerate(batch):
            cur.append(m)
            cur_tokens += est(m)
            if i == len(batch) - 1:
                break
            nxt = batch[i + 1]
            if (getattr(nxt, "type", None) == "tool"
                    or getattr(m, "tool_calls", None)):
                continue   # 不安全边界：继续累积到配对完整再切
            human_next = getattr(nxt, "type", None) == "human"
            if cur_tokens >= max_tokens * _CHUNK_FLOAT_LO and human_next:
                pass       # 用户消息前：优先收片（完整回合边界）
            elif cur_tokens >= max_tokens * _CHUNK_FLOAT_HI:
                pass       # 超出浮动上限：在安全边界兜底收片
            else:
                continue
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

    def _summarize_leaf(self, msgs, batch, removes, adds,
                        context_msgs=None, anchor_quote: str = "",
                        anchor_end_quote: str = "") -> str | None:
        """压缩一个叶子分片 → level0 节点；成功返回 node_id（失败 None）。

        - `batch`：本片（落节点 details、从 buffer 移除的都是它）；
        - `context_msgs`：发给压缩模型的**原始消息序列**——渐进前缀分片模式传
          "批次第 1 片到本片"（前面部分是背景，指令头/尾锚点标注本片范围）；
          默认 None = 只发本片（整批/独立分片）；
        - `anchor_quote` / `anchor_end_quote`：渐进前缀模式的头/尾锚点
          （本片首条有正文消息的开头 / 末条有正文消息的结尾）。
        """
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
        instruction = HumanMessage(
            content=self._leaf_instruction(start_pos, end_pos,
                                           anchor=anchor_quote,
                                           anchor_end=anchor_end_quote))
        send_raw = list(context_msgs) if context_msgs is not None else list(batch)
        # 图片块剥离后再发压缩调用（base64 对摘要无用且极贵）
        out = self._call_compress(self._strip_image_blocks(node_msgs + send_raw),
                                  instruction)
        if not out or not out.summary or not out.summary.strip():
            _logger.warning("[compress] 叶子摘要为空，跳过")
            return None
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
        return node_id

    @staticmethod
    def _anchor_quotes(chunk: list) -> tuple[str, str]:
        """分片锚点（头 + 尾）：供压缩指令"从这条消息开始…直到输入最后一条"定位。

        - 头 = 本片**第一条有正文消息**的开头 120 字（空 ai(tool_calls) 往前扫最多
          4 条找正文；全空退化为占位描述，极罕见）；
        - 尾 = 本片**最后一条有正文消息**的结尾 60 字（向后扫最多 4 条）——渐进
          前缀模式下分片永远是输入的末尾，尾部锚点防止模型把结尾的工具结果
          误当背景而提前收尾。
        """
        head = ""
        for m in chunk[:4]:
            text = content_text(getattr(m, "content", "") or "").strip()
            if text:
                head = text[:120]
                break
        if not head:
            names = [tc.get("name") for tc in (getattr(chunk[0], "tool_calls", None) or [])]
            head = ("（助手调用工具：" + "、".join(str(n) for n in names) + "）"
                    if names else "（本片开头）")
        tail = ""
        for m in reversed(chunk[-4:]):
            text = content_text(getattr(m, "content", "") or "").strip()
            if text:
                tail = text[-60:]
                break
        return head, tail

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
        # ⚠️ 目标也可能是**本轮刚产生的分片叶子**（消息在 adds、不在快照 msgs 里）
        # ——必须一并从 adds 摘除，否则子节点消息会和父节点消息并存于 buffer
        # （历史 bug：合并后 inactive 的 L0 与父节点同时可见、范围看起来重叠/
        # 不连续——用户实测 2026-10-19）
        adds[:] = [m for m in adds
                   if (getattr(m, "metadata", None) or {}).get("node_id") not in target_ids]
        # 目标之后的节点属于"未来"——**不进压缩输入**（与叶子压缩剔除保留尾部同一
        # 原则；防止模型把后面的状态混进本段摘要）。cutoff = 目标里最大的起始序号。
        cutoffs = []
        for n in to_compact:
            try:
                cutoffs.append(int(n["round_range"][0]))
            except (TypeError, ValueError, IndexError):
                pass
        cutoff = max(cutoffs) if cutoffs else None
        # 压缩输入边界（计划 7.2）：只放节点消息（原始消息不进）；目标节点从树渲染
        # → 去掉保护标记（模型据此知道"这几条要压"）；目标**之前**的节点保留标记
        # = 更早的背景、别动
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
                cs = (getattr(m, "metadata", None) or {}).get("node_start")
                if cutoff is not None and cs is not None:
                    try:
                        if int(cs) > cutoff:
                            continue   # 目标之后的节点（未来信息）→ 跳过
                    except (TypeError, ValueError):
                        pass
                context_msgs.append(m)          # 更早的节点：原样（带保护标记）
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
        """节点预算兜底裁剪（硬上限由压缩模型 max_tokens 承担）。

        预算口径 = **整节点目标字数**（中文设置值≈字数；见 `_budget_line`）。
        指令已要求按预算写，但模型经常写超 3~5 倍（用户实测 700 预算出 3~5k），
        所以这里做**总量硬兜底**：
        1) summary ≤ 60% 预算；
        2) 逐项限宽限数（单项 ≤200 字、≤8 条）；
        3) 总字数 > 1.1×预算时，按"最不关键→最关键"轮转删列表尾部
           （env → commands → artifacts → patterns → next_steps → todo →
           blockers → user → decisions → lessons.errors → constraints 最后），
           列表删空仍超才截 summary。
        """
        budget = max(200, int(budget or 1000))
        cap_summary = max(150, int(budget * 0.6))
        if len(out.summary) > cap_summary:
            out.summary = out.summary[:cap_summary].rstrip() + "…"

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

        def total_chars() -> int:
            n = len(out.summary) + len(t.goal) + len(t.status)
            for lst in (t.todo, t.next_steps, t.blockers,
                        c.env, c.commands, c.artifacts, c.constraints,
                        out.lessons.patterns, out.user):
                n += sum(len(str(x)) for x in lst)
            n += sum(len(d.what) + len(d.why) for d in out.decisions)
            n += sum(len(e.symptom) + len(e.cause) + len(e.fix)
                     for e in out.lessons.errors)
            return n

        limit = int(budget * 1.1)
        total = total_chars()
        # 按"最不关键→最关键"整组牺牲：前一组删空才动下一组，
        # 保证 constraints / decisions / lessons 尽量完整。
        groups = [c.env, c.commands, c.artifacts, out.lessons.patterns,
                  t.next_steps, t.blockers, out.user, t.todo,
                  out.decisions, out.lessons.errors, c.constraints]
        while total > limit:
            progressed = False
            for lst in groups:
                if total <= limit:
                    break
                while lst and total > limit:
                    lst.pop()
                    progressed = True
                    total = total_chars()
            if not progressed:
                break
        if total > limit:
            over = total - limit
            keep = max(120, len(out.summary) - over - 1)
            out.summary = out.summary[:keep].rstrip() + "…"
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
