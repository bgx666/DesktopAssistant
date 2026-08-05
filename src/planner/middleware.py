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


# ── 流式文本推送 ─────────────────────────────────────────────

class StreamTextMiddleware(AgentMiddleware):
    """after_model：每轮 LLM 返回后立刻 push_text（流式推送，不等 invoke 结束）。

    用 after_model（而非 wrap_model_call）——后者访问 response.result 可能消费
    一次性响应对象，导致框架检测不到 tool_calls → 工具不执行。
    """

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def after_model(self, state: PlannerState, runtime: Runtime) -> dict | None:
        try:
            msgs = state.get("messages") or []
            if not msgs:
                return None
            last = msgs[-1]
            if getattr(last, "type", None) != "ai":
                return None
            text = (getattr(last, "content", None) or "").strip()
            if text:
                self.session.push_text(text)
                self.session._save_log("assistant", text)
        except Exception:
            _logger.exception("[middleware] 流式文本推送失败")
        return None


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

_LEAF_INSTRUCTION = (
    "请把以下内容压缩提取关键信息。\n"
    "1. 关键信息要具体（任务、计划、决定、用户原话）\n"
    "2. 不要继续对话\n\n"
    "{original_text}\n"
    "请给出压缩结果："
)

_PARENT_INSTRUCTION = (
    "请把以下 {n} 段对话摘要的总体内容压缩提取关键信息。\n"
    "1. 关键信息要具体\n"
    "2. 不要继续对话\n\n"
    "{child_summaries}\n"
    "请给出压缩结果："
)


class SummarizationMiddleware(AgentMiddleware):
    """压缩 + 记忆树一体化（移植自 yaya YayaSummarizationMiddleware）。

    触发（before_model，每轮模型调用前，一次 invoke 最多一个压缩周期）：
      1) 原始消息数 ≥ SUMMARIZE_TRIGGER_MESSAGES → 叶子压缩（对话 → level0 节点）
      2) 树 level L 活跃节点 ≥ LEVEL_COMPACT_THRESHOLD → 向上压缩一层（递归）

    压缩请求（独立压缩模型执行，不污染主会话上下文）：
      [SystemMessage(角色卡)] + [主会话 buffer 快照原样] + [压缩提示 + 待压缩内容]

    落树：叶子 add_leaf（details 存原文，explore_memory_tree 可查）/ 父节点
    compact（子节点 is_active=0）；buffer：RemoveMessage 删被压缩消息 + 追加
    摘要消息（metadata["node_id"]，随 buffer 持久化）。
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

    def _summarize_leaf(self, msgs, batch, removes, adds) -> None:
        tree = self.session.get_memory_tree()
        pos = {id(m): i for i, m in enumerate(msgs)}
        start_pos = pos[id(batch[0])]
        end_pos = pos[id(batch[-1])]

        original_text = "\n".join(self._speaker_text(m) for m in batch)
        instruction = HumanMessage(
            content=_LEAF_INSTRUCTION.format(original_text=original_text))
        summary = self._call_compress(msgs, instruction)
        if not summary or not summary.strip():
            _logger.warning("[compress] 叶子摘要为空，跳过")
            return

        from langchain_core.messages import messages_to_dict
        node_id = tree.add_leaf(
            summary,
            (start_pos, end_pos),
            None,
            details=messages_to_dict(batch),
        )
        summary_msg = HumanMessage(
            content=f"[{node_id}] 第{start_pos}-{end_pos}条: {summary}",
            metadata={"node_id": node_id},
        )
        removes.extend(RemoveMessage(id=m.id) for m in batch)
        adds.append(summary_msg)
        _logger.info("[compress] 叶子 %s 第%d-%d条（%d 条消息落树）",
                     node_id, start_pos, end_pos, len(batch))

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
                rr = n.get("round_range") or ["?", "?"]
                child_msgs.append(HumanMessage(
                    content=f"[{n['id']}] 第{rr[0]}-{rr[1]}条: {n.get('summary', '')}",
                    metadata={"node_id": n["id"]}))

        child_summaries = "\n".join(
            f"节点 {n['id']}（第{n.get('round_range', ['?', '?'])[0]}-"
            f"{n.get('round_range', ['?', '?'])[1]}轮）: {n.get('summary', '')}"
            for n in to_compact)
        instruction = HumanMessage(
            content=_PARENT_INSTRUCTION.format(n=len(to_compact), child_summaries=child_summaries))
        parent_summary = self._call_compress(child_msgs, instruction)
        if not parent_summary or not parent_summary.strip():
            _logger.warning("[compress] 父节点摘要为空，停止向上压缩")
            return False

        parent_id = tree.compact([n["id"] for n in to_compact], parent_summary)
        rr0 = to_compact[0].get("round_range") or ["?", "?"]
        rr1 = to_compact[-1].get("round_range") or ["?", "?"]
        parent_msg = HumanMessage(
            content=f"[{parent_id}] 第{rr0[0]}-{rr1[1]}条: {parent_summary}",
            metadata={"node_id": parent_id},
        )
        removes.extend(RemoveMessage(id=m.id) for m in removable)
        adds.append(parent_msg)
        _logger.info("[compress] 父节点 %s（%d 个 level%d 节点合并）",
                     parent_id, len(to_compact), level)
        return True

    def _call_compress(self, context_msgs, instruction) -> str:
        """独立压缩模型调用：system(角色卡) + 上下文原样 + 指令（唯一新增）。"""
        from langchain_core.messages import SystemMessage
        model = self.session._get_summary_model()
        request = [SystemMessage(content=self.session.system_prompt)]
        request.extend(context_msgs)
        request.append(instruction)
        r = model.invoke(request)
        return (r.content or "").strip()

    @staticmethod
    def _speaker_text(m) -> str:
        role = {"human": "用户", "ai": "小助", "tool": "工具结果"}.get(m.type, m.type)
        return f"{role}: {m.content}"
