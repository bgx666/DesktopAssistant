package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage

/**
 * buffer 修复（移植 PC 版 `_repair_buffer` / `_strip_orphan_tool_messages` 的教训）。
 *
 * 为什么必须有：DeepSeek 要求 `assistant(tool_calls)` 后面**紧跟**每个 call id 的 tool 结果。
 * 两种情形会产生不配对的消息：
 * 1. 上一轮生成中途被杀（App 被系统回收 / 用户强杀）→ 末尾留下悬空的 tool_calls；
 * 2. 压缩/删除把一对切开 → 留下孤儿 tool 消息。
 * 任一种都会让**下一次请求直接 400**，而且是恢复上下文之后才发作，极难排查。
 *
 * 纯函数，可单测。
 */
object BufferRepair {

    fun repair(messages: List<ChatMessage>): List<ChatMessage> {
        if (messages.isEmpty()) return messages

        // 1) 被 tool 消息回答过的 call id
        val answered = messages.filter { it.role == "tool" }
            .mapNotNull { it.toolCallId }
            .toSet()

        // 2) 丢掉声明了 tool_calls 但没被完全回答的 assistant
        val keptAssistant = messages.filterNot { m ->
            val calls = m.toolCalls
            !calls.isNullOrEmpty() && calls.any { it.id !in answered }
        }

        // 3) 丢掉不再被任何 assistant 声明的 tool 消息
        val declared = keptAssistant
            .flatMap { it.toolCalls?.map { c -> c.id } ?: emptyList() }
            .toSet()
        return keptAssistant.filterNot { it.role == "tool" && (it.toolCallId ?: "") !in declared }
    }

    /** 末尾是否悬空（末尾是带 tool_calls 的 assistant）——落盘前可以据此判断是否需要修复。 */
    fun hasTrailingToolCalls(messages: List<ChatMessage>): Boolean =
        messages.lastOrNull()?.let { !it.toolCalls.isNullOrEmpty() } == true
}