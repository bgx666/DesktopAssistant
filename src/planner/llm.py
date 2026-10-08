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

from . import config as _config

_logger = logging.getLogger("planner.llm")


# ── 模型构建 ──────────────────────────────────────────────────

def resolve_model_name(model_name: str | None = None) -> str:
    """模型名解析链：显式参数 > PLANNER_LLM_MODEL > 默认 vision-exp。

    planner 默认用视觉模型 deepseek-v4-flash-vision-exp（多模态，文本能力与
    flash 持平）；共享 .env 的 LLM_MODEL 是 xiaob 游戏的配置，不再影响 planner
    （回退纯文本：settings.json 的 llm_model 或 PLANNER_LLM_MODEL=deepseek-v4-flash）。
    （env 在调用时读取——config 常量在 import 时绑定，测试 monkeypatch 不到。）
    """
    if model_name and str(model_name).strip():
        return str(model_name).strip()
    planner_model = os.getenv("PLANNER_LLM_MODEL", "").strip()
    if planner_model:
        return planner_model
    return _config.PLANNER_DEFAULT_MODEL


def is_vision_model(model_name: str) -> bool:
    """模型是否具备图像输入能力（按模型名启发式判定，覆盖常见命名）。"""
    name = (model_name or "").lower()
    return any(k in name for k in ("vision", "-vl", "-omni", "multimodal", "gpt-4o", "gpt-4.1"))


#: 思考开关的三个取值（官方 thinking.type，实测都有效）
THINKING_MODES = ("adaptive", "enabled", "disabled")
#: 思考强度的三个取值（官方 reasoning_effort）
REASONING_EFFORTS = ("low", "high", "max")


def is_deepseek_v4(model_name: str) -> bool:
    """是不是 DeepSeek v4 家族（决定发不发思考参数）。

    **不能只判断 `deepseek-v4` 前缀**：官方现在的主力 id 叫 `deepseek-flash`
    （正式名 DeepSeek-V4.1-Flash），前缀判断匹配不上它——
    用官方名时设置页里的思考开关会**静默失效**（测试抓到的就是这个）。
    """
    name = (model_name or "").strip().lower()
    return (
        name.startswith("deepseek-v4")
        or name == "deepseek-flash"
        or name.startswith("deepseek-flash-")
    )


def thinking_extra_body(model_name: str,
                        thinking_mode: str | None = None,
                        reasoning_effort: str | None = None) -> dict:
    """DeepSeek v4 的思考参数（**纯函数**，方便直接测）。

    单独抽出来的原因：LangChain 把 `extra_body` 交给底层 client 在请求时合并，
    实例上看不到这个属性——想断言"到底发了什么"就只能靠这个函数。

    规则：
    - 只有 DeepSeek v4 家族发这两个字段（别的服务商不认，发了会 400）
    - 取值非法就回落到默认（adaptive / high），不让设置页的脏值传到线上
    - `disabled` 时**不发** `reasoning_effort`：官方会忽略它，
      发了只会让人误以为强度还在起作用
    """
    if not is_deepseek_v4(model_name):
        return {}
    mode = (thinking_mode or "adaptive").strip().lower()
    if mode not in THINKING_MODES:
        mode = "adaptive"
    effort = (reasoning_effort or "high").strip().lower()
    if effort not in REASONING_EFFORTS:
        effort = "high"
    extra: dict = {"thinking": {"type": mode}}
    if mode != "disabled":
        extra["reasoning_effort"] = effort
    return extra


def build_chat_model(api_key: str | None = None,
                     base_url: str | None = None,
                     model_name: str | None = None,
                     thinking_mode: str | None = None,
                     reasoning_effort: str | None = None) -> ChatOpenAI:
    """DeepSeek v4 适配的 ChatOpenAI。

    配置优先级：显式参数（用户设置）> PLANNER_LLM_MODEL > LLM_MODEL > 默认 vision-exp。

    思考开关与强度来自设置页（`settings.py` 的 `thinking_mode` / `reasoning_effort`），
    拼装逻辑见 [thinking_extra_body]。实测要点（2026-09-30，DeepSeek 官方）：
    - `disabled` 时 reasoning token **真的是 0**，同一道题快约 30%
    - 曾经这里写死 `reasoning_effort: "low"`（想更快更省），但用一道**真实的方案题**
      实测发现 low 反而推理更多、答得更短、还更慢（3005/1604字/20.5s vs
      high 的 1645/2222字/15.4s）。所以现在默认 `high`，并把选择权交给设置页
    - 不再显式发 `thinking: enabled`：默认就在推理，显式打开没收益
    """
    model_name = resolve_model_name(model_name)
    base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
    api_key = api_key or os.getenv("LLM_API_KEY")
    kwargs: dict[str, Any] = {}
    extra = thinking_extra_body(model_name, thinking_mode, reasoning_effort)
    if extra:
        kwargs["extra_body"] = extra
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
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
        # 模拟 continue_speaking：上一轮调用了分段工具 → 本轮纯文本收尾（无工具调用）
        for m in messages:
            if (getattr(m, "type", None) == "tool"
                    and "继续" in str(m.content or "")):
                return self._make_result("（这是分点说法的最后一点，说完了。）")
        # 玩家要求分点描述 → 先调 continue_speaking 说第一点（循环继续）
        # （用所有 HumanMessage 判断：计划快照会注入"（当前计划）"Human 且以 [ 开头）
        user_texts = " ".join(
            str(m.content or "") for m in messages if isinstance(m, HumanMessage))
        if "分点" in user_texts or "continue" in user_texts:
            return self._make_result("第一点：先梳理整体目标。",
                                     [self._tc("continue_speaking", {})])
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

    @staticmethod
    def _upcoming(db) -> list[dict]:
        """脚本用：今天起 7 天内的日历项（读失败就当空，别让 mock 把测试带崩）。"""
        from datetime import datetime, timedelta, timezone
        try:
            today = datetime.now(timezone(timedelta(hours=8))).date()
            return db.list_calendar_range(today.isoformat(),
                                          (today + timedelta(days=7)).isoformat())
        except Exception:
            return []

    def _player_reply(self) -> ChatResult:
        calls = [self._tc("heartbeat", {"minutes": 15})]
        session = self._session
        db = getattr(session, "db", None) if session else None
        text = self._rng.choice(_PLAYER_REPLIES)
        if db is not None and not self._upcoming(db):
            text = "（用户还没安排什么，我先主动把学习排进日历。）"
            calls.insert(0, self._tc("add_schedule", {
                "date": self._today(),
                "content": "复习线性代数第三章",
                "time": "20:00",
                "detail": "矩阵与行列式，重点：逆矩阵与秩",
            }))
        return self._make_result(text, calls)

    def _heartbeat_reply(self) -> ChatResult:
        calls = []
        text = "（我看了看日历，一切照常推进。）"
        session = self._session
        db = getattr(session, "db", None) if session else None
        if db is not None:
            try:
                items = self._upcoming(db)
                todo = [i for i in items if i["status"] == "todo"]
                if not items:
                    text = "（日历上还是空的，我排一条进去。）"
                    calls.append(self._tc("add_schedule", {
                        "date": self._today(),
                        "content": "复习线性代数第三章",
                        "time": "20:00",
                    }))
                elif todo:
                    text = "（日历上还有没做的，先勾掉最早的一项。）"
                    calls.append(self._tc("update_schedule",
                                          {"item_id": todo[0]["id"], "status": "done"}))
                else:
                    text = "（日历上的安排都做完了，休息一下。）"
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
