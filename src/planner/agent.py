"""create_agent 构建（LangGraph 化核心，移植自 yaya backend agent.py）。

- build_planner_agent(session)：create_agent(model, tools, system_prompt, middleware,
  state_schema) → 编译后的 agent 图；
- 循环（model → tool_calls → tools → model …直到无工具调用）由 create_agent 内建，
  heartbeat 工具即停止信号；
- **不使用 checkpointer**：invoke 输入即初始 state（无跨调用消息合并）——DeepSeek
  严格校验「assistant(tool_calls) 必须紧跟对应 tool 消息」，checkpointer 的
  add_messages 合并会保留中断时半写的孤儿 assistant 导致 400（yaya 实测）。
  buffer 持久化由 session._save_buffer_state（messages_to_dict → json）承担；
- mock 模式 model=MockChatModel，真实 = ChatOpenAI（llm.py::build_chat_model）；
  模式切换/桩注入时重建（session.toggle_mock / set_chat_model）。
"""

from __future__ import annotations

import logging

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware

from .middleware import (
    DndGuardMiddleware,
    HeartbeatTrackMiddleware,
    LoggingMiddleware,
    NudgeMiddleware,
    PlanSnapshotMiddleware,
    PlannerState,
    PlayerPriorityMiddleware,
    SummarizationMiddleware,
)
from .tools import build_tools

_logger = logging.getLogger("planner.agent")


def build_planner_agent(session):
    """构建编译后的 agent 图（每次模式切换/模型注入时重建）。

    流式输出由 session._run_agent 的 agent.stream（stream_mode=["messages","updates"]）
    承担（text_stream 逐字推送 + text 完整文本），不再需要 StreamTextMiddleware。
    """
    model = session._get_llm()  # mock=MockChatModel / 真实=ChatOpenAI
    middleware = [
        DndGuardMiddleware(session),
        PlanSnapshotMiddleware(session),
        PlayerPriorityMiddleware(session),
        NudgeMiddleware(),
        HeartbeatTrackMiddleware(),
        LoggingMiddleware(session),
        SummarizationMiddleware(session),
        ModelCallLimitMiddleware(run_limit=10, exit_behavior="end"),
    ]
    agent = create_agent(
        model=model,
        tools=build_tools(session),
        system_prompt=session.system_prompt,
        middleware=middleware,
        state_schema=PlannerState,
    )
    _logger.info("[agent] 已构建 %s 模式 agent（%d 个中间件）", session.mode, len(middleware))
    return agent
