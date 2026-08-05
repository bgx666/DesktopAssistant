"""planner —— 学习工作助手 Agent。

架构（参考 yaya backend / xiaob 记忆树）：
- 后端：Python + LangChain create_agent + 中间件链 + 后端调度线程
- 存储：planner.db（任务/阶段/日计划）+ memory_tree.db（对话分层摘要树）
- 前端：Electron 悬浮窗（frontend/），轮询 /dequeue
"""

__version__ = "0.1.0"
