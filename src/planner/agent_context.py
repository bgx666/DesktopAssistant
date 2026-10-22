"""当前 Agent 上下文（主助手 / White）。

工具、审批、变更留底都要知道"这条操作是谁发起的"：
- 主助手：线程里没设置 → 默认 "main"；
- White：运行线程启动时 set_current_agent("white")（线程内 contextvar 隔离，
  不会影响主助手线程）。
"""

from __future__ import annotations

import contextvars

_current = contextvars.ContextVar("planner_agent", default="main")


def current_agent() -> str:
    """当前线程所属的 agent id（main / white）。"""
    return _current.get()


def set_current_agent(agent_id: str):
    """设置当前线程的 agent id（返回 token，便于恢复）。"""
    return _current.set(str(agent_id or "main"))
