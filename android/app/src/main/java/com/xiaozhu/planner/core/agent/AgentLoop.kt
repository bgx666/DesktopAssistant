package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.LlmEvent
import com.xiaozhu.planner.core.llm.LlmStreamer
import com.xiaozhu.planner.core.llm.ToolCall
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

/** 一轮 ReAct 对外暴露的事件（UI 直接照着画）。 */
sealed interface AgentEvent {
    data class TextDelta(val text: String) : AgentEvent
    data class ToolStarted(val name: String, val args: String) : AgentEvent
    data class ToolFinished(val name: String, val result: String) : AgentEvent
    data class Done(val text: String) : AgentEvent
    data class Error(val message: String) : AgentEvent
}

/**
 * 原生多轮 ReAct：模型 → 工具 → 模型 …… 直到模型不再要求调用工具。
 *
 * [history] 会被就地追加（assistant 消息与其 tool 结果成对写入）——
 * 这是 DeepSeek 的硬约束：assistant 里声明了 tool_calls，就必须跟上每个 id 的 tool 结果，
 * 否则下一轮 400。
 */
class AgentLoop(
    private val llm: LlmStreamer,
    private val registry: ToolRegistry,
    private val maxRounds: Int = 10,
) {

    fun run(history: MutableList<ChatMessage>): Flow<AgentEvent> = flow {
        for (round in 1..maxRounds) {
            val text = StringBuilder()
            val calls = mutableListOf<ToolCall>()
            var failure: String? = null

            llm.stream(history.toList(), registry.specs()).collect { event ->
                when (event) {
                    is LlmEvent.TextDelta -> {
                        text.append(event.text)
                        emit(AgentEvent.TextDelta(event.text))
                    }
                    is LlmEvent.ToolCalls -> calls += event.calls
                    is LlmEvent.Finished -> Unit
                    is LlmEvent.Failed -> failure = event.message
                }
            }

            failure?.let {
                emit(AgentEvent.Error(it))
                return@flow
            }

            if (calls.isEmpty()) {
                history += ChatMessage.assistant(text.toString())
                emit(AgentEvent.Done(text.toString()))
                return@flow
            }

            // 先落下 assistant（带 tool_calls），再逐个补 tool 结果 —— 顺序不能反
            history += ChatMessage.assistantToolCalls(calls, text.toString())
            for (call in calls) {
                emit(AgentEvent.ToolStarted(call.function.name, call.function.arguments))
                val result = registry.execute(call)
                emit(AgentEvent.ToolFinished(call.function.name, result))
                history += ChatMessage.tool(call.id, call.function.name, result)
            }
        }
        emit(AgentEvent.Error("连续 $maxRounds 轮都在调用工具，已停下（可能陷入了循环）。"))
    }
}