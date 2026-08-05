"""LLM 层（langchain）。

- build_chat_model()：ChatOpenAI（DeepSeek v4 适配：extra_body 传 thinking/reasoning_effort，
  不能用 model_kwargs——openai SDK 2.52 会把未知参数展开为 create() 命名参数而 TypeError；
  与 yaya backend 相同配置路径，配置来自 .env / D:\\xiaob\\.env）；
- MockChatModel：脚本化假 LLM（BaseChatModel），不调真实 API，供测试与无 key 联调。
  脚本逻辑驱动真实工具执行，形成「建任务 → 拆解 → 勾选完成」的自循环演示。
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

_logger = logging.getLogger("planner.llm")


# ── 模型构建 ──────────────────────────────────────────────────

def build_chat_model() -> ChatOpenAI:
    """DeepSeek v4 适配的 ChatOpenAI。配置来自 .env / D:\\xiaob\\.env。"""
    model_name = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    kwargs: dict[str, Any] = {}
    if model_name.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    return ChatOpenAI(
        model=model_name,
        api_key=os.getenv("LLM_API_KEY"),
        base_url=base_url,
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
        timeout=int(os.getenv("LLM_TIMEOUT", "60")),
        **kwargs,
    )


# ── MockChatModel（脚本化假 LLM）─────────────────────────────

class MockChatModel(BaseChatModel):
    """脚本化假 LLM（BaseChatModel 子类）：不调真实 API，走同一 agent graph。

    脚本逻辑（依据 session 的任务库状态决策）：
    - 玩家消息 → 文本回复 + heartbeat(minutes=15)；
    - 心跳消息 → 按需轮转：建任务 / 拆解任务 / 勾选今日计划 / 更新任务状态，
      每轮都以 heartbeat 收尾（self-healing 循环，可无 API 演示完整链路）。
    """

    def __init__(self, session=None, **kwargs: Any) -> None:
        super().__init__(model="mock-planner", **kwargs)
        self._session = session
        self._heartbeat_count = 0
        self._rng = random.Random()
        self._uuid = uuid

    def reset(self) -> None:
        self._heartbeat_count = 0

    @property
    def _llm_type(self) -> str:
        return "mock-planner"

    def bind_tools(self, tools, **kwargs):
        """记录绑定的工具名并返回 self（create_agent 每次模型调用都会重新 bind_tools，
        不能新建副本——否则 _heartbeat_count 等脚本状态每次调用都会丢失）。"""
        self._bound_tools = [t.name for t in (tools or []) if hasattr(t, "name")]
        return self

    # ── BaseChatModel 实现 ───────────────────────────────────

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_user = self._last_user_content(messages)
        has_tools = bool(getattr(self, "_bound_tools", None) or kwargs.get("tools"))
        if not has_tools:
            # 压缩等独立调用：输出结构化 JSON（summary + profile + future_notes）
            return self._make_result(json.dumps({
                "summary": "（摘要）助手和用户讨论了任务安排，并按计划推进。",
                "profile": {
                    "preferences": ["喜欢晚上学习"],
                    "personality": ["做事有计划"],
                    "habits": [],
                    "goals": ["按时完成学习计划"],
                },
                "future_notes": [],
            }, ensure_ascii=False))
        if last_user.startswith(("（", "[", "【")):
            return self._heartbeat_reply()
        return self._player_reply()

    # ── 脚本 ─────────────────────────────────────────────────

    @staticmethod
    def _last_user_content(messages) -> str:
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                return str(m.content or "")
        return ""

    def _today(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def _player_reply(self) -> ChatResult:
        calls = [self._tc("heartbeat", {"minutes": 15})]
        session = self._session
        db = getattr(session, "db", None) if session else None
        text = self._rng.choice(_PLAYER_REPLIES)
        if db is not None and not db.list_tasks():
            text = "（用户还没给我任务，我先主动列一个学习任务。）"
            calls.insert(0, self._tc("create_task", {
                "title": "复习线性代数第三章",
                "description": "矩阵与行列式，重点：逆矩阵与秩",
                "due_date": self._today(),
                "priority": "high",
            }))
        return self._make_result(text, calls)

    def _heartbeat_reply(self) -> ChatResult:
        calls = []
        text = "（我看了看手头的计划，一切照常推进。）"
        session = self._session
        db = getattr(session, "db", None) if session else None
        if db is not None:
            try:
                tasks = db.list_tasks()
                if not tasks:
                    text = "（我列了一个新的学习任务，先记下来。）"
                    calls.append(self._tc("create_task", {
                        "title": "复习线性代数第三章",
                        "description": "矩阵与行列式，重点：逆矩阵与秩",
                        "due_date": self._today(),
                        "priority": "high",
                    }))
                else:
                    pending = db.list_pending()
                    todo = [t for t in tasks if t["status"] in ("todo", "in_progress")]
                    if pending:
                        text = "（待办队列里还有没做的，我先勾掉最早的一项。）"
                        calls.append(self._tc("mark_plan_done", {"plan_id": pending[0]["id"]}))
                    elif any(t["status"] == "todo" for t in todo):
                        text = "（有条任务还没有拆解，我来安排一下。）"
                        t = next(t for t in todo if t["status"] == "todo")
                        calls.append(self._tc("break_down_task", {
                            "task_id": t["id"],
                            "phases": [
                                {"title": "概念梳理", "days": 1,
                                 "items": [{"date_offset": 0, "content": f"通读{t['title']}教材章节"}]},
                                {"title": "习题巩固", "days": 1,
                                 "items": [{"date_offset": 1, "content": "完成章节习题前半"}]},
                            ],
                        }))
                    else:
                        text = "（所有任务都完成了，休息一下。）"
            except Exception:
                _logger.exception("[mock] 脚本状态读取失败")
        calls.append(self._tc("heartbeat", {"minutes": 15}))
        return self._make_result(text, calls)

    # ── 构造工具调用 ─────────────────────────────────────────

    def _tc(self, name: str, args: dict) -> dict:
        return {"name": name, "args": args, "id": f"mockcall_{self._uuid.uuid4().hex[:12]}",
                "type": "tool_call"}

    def _make_result(self, content: str, tool_calls: list[dict] | None = None) -> ChatResult:
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=content, tool_calls=tool_calls or []))
        ])


# ── mock 脚本素材 ─────────────────────────────────────────

_PLAYER_REPLIES = [
    "收到，我看看今天的计划安排，稍后跟你同步。",
    "好的，我先把任务记下来，拆好计划再告诉你。",
    "明白，我会盯进度，到点提醒你。",
    "没问题，先把手头的活干完，回头我再跟你确认。",
]
