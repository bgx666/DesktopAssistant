package com.xiaozhu.planner.core.llm

/** 非流式一问一答（压缩摘要用；不参与对话上下文，也不显示给用户）。 */
interface LlmCompleter {
    suspend fun complete(messages: List<ChatMessage>): String
}