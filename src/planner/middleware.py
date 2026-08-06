"""planner 业务中间件（LangChain AgentMiddleware，移植自 yaya backend）。

钩子语义：
- node-style（before_agent / before_model / after_model）：顺序执行，返回 dict 合并进
  state（走 reducer），支持 {"jump_to": "end"} 提前跳转；
- wrap-style（wrap_model_call / wrap_tool_call）：包裹每次调用，可短路/重试/转换。

中间件链（agent.py 中按序）：
- DndGuardMiddleware      before_agent   免打扰时段且非玩家触发 → 跳 end
- PlanSnapshotMiddleware  before_agent   计划快照有变化 → 注入 [当前计划] 消息
- PlayerPriorityMiddleware before_model   玩家消息在 _inbox → 跳 end（让位）
- NudgeMiddleware         after_model 计数 + before_model 第 5 轮注入「走神」提示
- HeartbeatTrackMiddleware wrap_tool_call 记录 + before_model 跳转（heartbeat 即停）
- StreamTextMiddleware    after_model   每轮 LLM 返回立刻 push_text（流式）
- LoggingMiddleware       wrap_*        每轮 LLM/工具调用写 jsonl 日志
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.messages import HumanMessage, RemoveMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from typing_extensions import NotRequired

_logger = logging.getLogger("planner.middleware")


class PlannerState(AgentState):
    """planner agent 的 state 扩展字段。"""

    model_call_count: NotRequired[int]
    set_heartbeat_called: NotRequired[bool]
    plan_injected: NotRequired[bool]


# ── 免打扰守卫 ───────────────────────────────────────────────

class DndGuardMiddleware(AgentMiddleware):
    """before_agent：免打扰时段内，自主触发（非玩家消息）→ 跳 end。

    玩家消息永远不受限（用户主动找你肯定要回）。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: PlannerState, runtime: Runtime) -> dict | None:
        if self.session.in_dnd() and self.session.current_trigger != "player":
            _logger.info("[dnd] 免打扰时段，跳过自主触发")
            self.session.push_log("（免打扰时段，暂不打扰。）")
            return {"jump_to": "end"}
        return None


# ── 计划快照注入 ─────────────────────────────────────────────

class PlanSnapshotMiddleware(AgentMiddleware):
    """before_agent：计划快照指纹变化时注入 [当前计划] 文本（每次生成最多注入一次）。"""

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: PlannerState, runtime: Runtime) -> dict | None:
        text = self.session._plan_snapshot_text()
        if text is None:
            return None
        return {"messages": [HumanMessage(content=text)]}


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
    """after_model 递增 model_call_count；before_model 在第 5 轮后注入收尾提示。"""

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
        response = handler(request)
        try:
            ai = response.result
            if isinstance(ai, list):
                ai = ai[0] if ai else None
            tcs = [t.get("name") for t in (ai.tool_calls or [])] if ai is not None else []
            self.session._save_log("llm_call", json.dumps({
                "msgs": len(request.messages),
                "content": self._shorten((ai.content or "") if ai is not None else ""),
                "tool_calls": tcs,
            }, ensure_ascii=False))
        except Exception:
            _logger.exception("[middleware] llm 日志失败")
        return response

    def wrap_tool_call(self, request, handler: Callable):
        try:
            self.session._save_log(
                "tool_call",
                f"{request.tool_call['name']}({json.dumps(request.tool_call.get('args', {}), ensure_ascii=False)})",
            )
        except Exception:
            _logger.exception("[middleware] tool 日志失败")
        return handler(request)


# ── 压缩 + 记忆树 ────────────────────────────────────────────

SUMMARIZE_TRIGGER_MESSAGES = 60
SUMMARIZE_KEEP_MESSAGES = 20
LEVEL_COMPACT_THRESHOLD = 6
BRANCHING_FACTOR = 3

from pydantic import BaseModel, Field  # noqa: E402


class ProfileInfo(BaseModel):
    """节点中提炼的用户画像（无画像信息则为空数组）。"""
    preferences: list[str] = Field(default_factory=list, description="喜好与偏好：用户喜欢/偏爱什么")
    personality: list[str] = Field(default_factory=list, description="性格特点：行为模式、处事风格")
    habits: list[str] = Field(default_factory=list, description="习惯与作息：规律性行为")
    goals: list[str] = Field(default_factory=list, description="目标与动机：为什么做、想达成什么")


class MemoryNodeOutput(BaseModel):
    """压缩节点的 LLM 结构化输出（叶子与父节点共用）。

    字段演进约定：新增字段改本 model + 提示词即可，存储零迁移
    （meta 为 JSON 扩展位；extra=ignore 丢弃未知字段）。
    """

    model_config = {"extra": "ignore"}

    summary: str = Field(description="内容摘要：只总结事件/决定/对话进程，不要包含用户画像信息")
    profile: ProfileInfo = Field(default_factory=ProfileInfo)
    future_notes: list[str] = Field(
        default_factory=list,
        description="后续说明：参考待压缩区域之后的原始消息，对摘要/画像中的内容做出的"
                    "澄清/修正/补全；仅当后续消息直接相关时填写，否则留空")
    meta: dict = Field(default_factory=dict,
                       description="节点元信息：schema_version 与未来扩展字段")


_LEAF_INSTRUCTION = (
    "请把以下内容压缩为结构化 JSON（只输出 JSON，不要输出其他文字）。\n"
    "**各字段按实际内容填写：没有相关信息的字段保持空（profile 各维度留空数组、"
    "future_notes 留空数组），不要为了填充而编造、猜测或重复已有内容。**\n"
    "字段说明：\n"
    "1. summary：内容摘要——只总结事件、决定、对话进程；不要包含用户画像信息。\n"
    "   **工具结果要高度压缩**：待压缩内容里的「工具结果」往往是工具返回的原文"
    "（如探索记忆树返回的旧对话、读取文件返回的内容），summary 只需提炼其中对"
    "后续对话有实际价值的信息（关键结论、数据、决定、用户原话要点），一两句话概括"
    "「小助从中获得了什么」，**不要转述、复述或浓缩工具返回的原文细节**——"
    "原文已随节点保存，摘要里不需要承载它。\n"
    "2. profile：用户画像——从这段对话中提炼用户特征，没有相关信息则对应维度留空数组：\n"
    "   preferences 喜好偏好 / personality 性格特点 / habits 习惯作息 / goals 目标动机\n"
    "   可参考上下文里已有节点的画像：延续已有认知、补充新观察，不重复不矛盾\n"
    "3. future_notes：后续说明——查看待压缩区域之后的原始消息，仅当后续消息直接解释/"
    "澄清/修正/补全了本段内容时填写（注明对应本段的哪个点 + 后续消息怎么说）；"
    "无关的未来消息不填；本段之后没有原始消息则留空\n\n"
    "待压缩内容：\n"
    "{original_text}\n"
    "请给出压缩结果："
)

_PARENT_INSTRUCTION = (
    "请把以下 {n} 个节点压缩为结构化 JSON（只输出 JSON，不要输出其他文字）。\n"
    "**各字段按实际内容填写：没有相关信息的字段保持空（profile 各维度留空数组、"
    "future_notes 留空数组），不要为了填充而编造、猜测或重复已有内容。**\n"
    "字段说明：\n"
    "1. summary：对子节点摘要的提炼\n"
    "2. profile：对子节点画像的提炼上卷（同维度合并归纳，没有相关信息则对应维度留空数组）：\n"
    "   preferences 喜好偏好 / personality 性格特点 / habits 习惯作息 / goals 目标动机\n"
    "3. future_notes：子节点的后续说明若已被父级摘要/画像吸收则不必重复；"
    "若有新的、未被吸收的澄清再填写\n\n"
    "节点信息：\n"
    "{child_summaries}\n"
    "请给出压缩结果："
)


class SummarizationMiddleware(AgentMiddleware):
    """压缩 + 记忆树一体化（移植自 yaya YayaSummarizationMiddleware，扩展画像字段）。

    触发（before_model，每轮模型调用前，一次 invoke 最多一个压缩周期）：
      1) 原始消息数 ≥ SUMMARIZE_TRIGGER_MESSAGES → 叶子压缩（对话 → level0 节点）
      2) 树 level L 活跃节点 ≥ LEVEL_COMPACT_THRESHOLD → 向上压缩一层（递归）

    压缩请求（独立压缩模型执行，不污染主会话上下文）：
      [SystemMessage(角色卡)] + [主会话 buffer 快照原样] + [压缩提示 + 待压缩内容]
      ——前缀与主对话一致 → 服务端 prompt caching 命中；快照含"未来"原始消息，
      future_notes 字段据此对摘要/画像做澄清。

    输出：MemoryNodeOutput（summary + profile + future_notes），经
    with_structured_output(json_mode) 主通道 / model_validate_json 降级解析。

    落树：叶子 add_leaf（details 存原文 + future_notes；profile 存画像）/ 父节点
    compact（profile 上卷；子节点 is_active=0）；buffer：RemoveMessage 删被压缩
    消息 + 追加全字段节点消息（metadata["node_id"]，随 buffer 持久化）。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def before_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        try:
            return self._maybe_compress(state)
        except Exception:
            _logger.exception("[middleware] 压缩异常（跳过本轮压缩）")
            return None

    def _maybe_compress(self, state: PlannerState) -> dict | None:
        msgs = list(state.get("messages") or [])
        removes: list[RemoveMessage] = []
        adds: list[HumanMessage] = []

        raw = [m for m in msgs if "node_id" not in (getattr(m, "metadata", None) or {})]
        if len(raw) >= SUMMARIZE_TRIGGER_MESSAGES:
            batch = raw[:len(raw) - SUMMARIZE_KEEP_MESSAGES]
            if batch:
                self._summarize_leaf(msgs, batch, removes, adds)

        tree = self.session.get_memory_tree()
        level = 0
        while tree.get_level_count(level) >= LEVEL_COMPACT_THRESHOLD:
            if not self._compact_level(msgs, level, removes, adds):
                break
            level += 1

        if removes or adds:
            return {"messages": removes + adds}
        return None

    # ── 节点消息渲染（全字段，供模型上下文与 explore 使用）──────

    @staticmethod
    def _render_node_text(node_id: str, start, end, out: MemoryNodeOutput) -> str:
        lines = [f"[{node_id}] 第{start}-{end}条", f"[摘要] {out.summary}"]
        p = out.profile
        for label, items in (("喜好", p.preferences), ("性格", p.personality),
                             ("习惯", p.habits), ("目标", p.goals)):
            if items:
                lines.append(f"[{label}] " + "、".join(items))
        if out.future_notes:
            lines.append("[后续说明] " + "；".join(out.future_notes))
        return "\n".join(lines)

    @staticmethod
    def _node_full_text(n: dict) -> str:
        """把树节点 dict（get_nodes_at_level 返回）渲染为全字段文本。"""
        rr = n.get("round_range") or ["?", "?"]
        lines = [f"节点 {n['id']}（第{rr[0]}-{rr[1]}轮）", f"摘要：{n.get('summary', '')}"]
        p = n.get("profile") or {}
        for label, key in (("喜好", "preferences"), ("性格", "personality"),
                           ("习惯", "habits"), ("目标", "goals")):
            items = p.get(key) or []
            if items:
                lines.append(f"{label}：{'、'.join(items)}")
        details = n.get("details")
        if isinstance(details, dict) and details.get("future_notes"):
            lines.append("后续说明：" + "；".join(details["future_notes"]))
        return "\n".join(lines)

    # ── 叶子压缩（对话 → level0 节点）────────────────────────

    def _summarize_leaf(self, msgs, batch, removes, adds) -> None:
        tree = self.session.get_memory_tree()
        # 全局序号（对齐小B _span 机制）：节点范围 = 已压缩累计条数起，
        # 连续不重叠——不能用 enumerate(msgs) 相对索引（每次从 0 附近开始，
        # 多次压缩的节点范围看起来全是从"第0条"开始，假重叠）
        start_pos = self.session._compressed_total
        end_pos = start_pos + len(batch) - 1

        original_text = "\n".join(self._speaker_text(m) for m in batch)
        instruction = HumanMessage(
            content=_LEAF_INSTRUCTION.format(original_text=original_text))
        out = self._call_compress(msgs, instruction)
        if not out or not out.summary or not out.summary.strip():
            _logger.warning("[compress] 叶子摘要为空，跳过")
            return

        from langchain_core.messages import messages_to_dict
        node_id = tree.add_leaf(
            out.summary,
            (start_pos, end_pos),
            None,
            details=messages_to_dict(batch),
            profile=self._profile_or_none(out),
            future_notes=out.future_notes or None,
            meta={"schema_version": 1, **out.meta},
        )
        self.session._compressed_total = end_pos + 1   # 累加已压缩条数
        node_text = self._render_node_text(node_id, start_pos, end_pos, out)
        # node_start：全局起始序号——回写 buffer 后据此把节点移到
        # 正确位置（压缩的总是最早的消息，节点应排在 buffer 最前）
        summary_msg = HumanMessage(
            content=node_text,
            metadata={"node_id": node_id, "node_start": start_pos})
        removes.extend(RemoveMessage(id=m.id) for m in batch)
        adds.append(summary_msg)
        _logger.info("[compress] 叶子 %s 第%d-%d条（%d 条消息落树，画像维度=%s）",
                     node_id, start_pos, end_pos, len(batch),
                     len(out.profile.model_dump(exclude_none=True)))

    # ── 向上压缩（递归：摘要还能再被摘要）────────────────────

    def _compact_level(self, msgs, level, removes, adds) -> bool:
        tree = self.session.get_memory_tree()
        nodes = tree.get_nodes_at_level(level)
        if len(nodes) < LEVEL_COMPACT_THRESHOLD:
            return False
        to_compact = nodes[:BRANCHING_FACTOR]

        by_id = {}
        for m in msgs:
            nid = (getattr(m, "metadata", None) or {}).get("node_id")
            if nid:
                by_id[nid] = m
        child_msgs = []
        removable = []
        for n in to_compact:
            m = by_id.get(n["id"])
            if m is not None:
                child_msgs.append(m)
                removable.append(m)
            else:
                child_msgs.append(HumanMessage(
                    content=self._node_full_text(n),
                    metadata={"node_id": n["id"]}))

        child_summaries = "\n".join(self._node_full_text(n) for n in to_compact)
        instruction = HumanMessage(
            content=_PARENT_INSTRUCTION.format(n=len(to_compact), child_summaries=child_summaries))
        out = self._call_compress(child_msgs, instruction)
        if not out or not out.summary or not out.summary.strip():
            _logger.warning("[compress] 父节点摘要为空，停止向上压缩")
            return False

        parent_id = tree.compact(
            [n["id"] for n in to_compact], out.summary,
            profile=self._profile_or_none(out),
            meta={"schema_version": 1, **out.meta})
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
        parent_text = self._render_node_text(parent_id, rr0[0], rr1[1], out)
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
    def _profile_or_none(out: MemoryNodeOutput) -> dict | None:
        profile = out.profile.model_dump()
        return profile if any(profile.values()) else None

    # ── 压缩请求 ────────────────────────────────────────────

    def _call_compress(self, context_msgs, instruction) -> MemoryNodeOutput | None:
        """独立压缩模型调用：system(角色卡) + 上下文原样 + 指令（唯一新增）。

        主通道：with_structured_output(json_mode)——JSON Schema 保证输出可解析；
        降级 1：文本 JSON model_validate_json；降级 2：纯摘要兜底。
        """
        from langchain_core.messages import SystemMessage
        model = self.session._get_summary_model()
        request = [SystemMessage(content=self.session.system_prompt)]
        request.extend(context_msgs)
        request.append(instruction)
        try:
            structured = model.with_structured_output(MemoryNodeOutput, method="json_mode")
            out = structured.invoke(request)
            if isinstance(out, MemoryNodeOutput):
                return out
            if isinstance(out, dict):
                return MemoryNodeOutput.model_validate(out)
        except Exception:
            _logger.warning("[compress] with_structured_output 失败，降级解析")
        try:
            r = model.invoke(request)
            text = (r.content or "").strip()
            return MemoryNodeOutput.model_validate_json(text)
        except Exception:
            _logger.warning("[compress] JSON 解析失败，降级为纯摘要")
            try:
                r = model.invoke(request)
                text = (r.content or "").strip()
            except Exception:
                return None
            return MemoryNodeOutput(summary=text) if text else None

    @staticmethod
    def _speaker_text(m) -> str:
        role = {"human": "用户", "ai": "小助", "tool": "工具结果"}.get(m.type, m.type)
        return f"{role}: {m.content}"
