package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.flow.Flow

/**
 * AgentLoop 依赖的抽象。真实实现是 [OpenAiClient]；单测里替换成脚本化假实现——
 * 这样 ReAct 循环（工具配对、最大轮数、错误处理）能在 JVM 上验，不碰网络。
 */
interface LlmStreamer {
    fun stream(messages: List<ChatMessage>, tools: List<ToolSpec>): Flow<LlmEvent>
}