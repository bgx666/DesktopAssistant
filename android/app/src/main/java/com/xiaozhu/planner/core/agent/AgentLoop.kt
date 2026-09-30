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
    /** `continue_speaking` 的节奏依据：进入工具调用前，把这一段说了多少字记下来。 */
    private val pacing: SpeechPacing = SpeechPacing(),
    /**
     * "下一轮要注入的图片"暂存槽（`show_image` 工具往里放）。
     *
     * 与 PC 的 `_pending_screenshots` + `ScreenShotInjectMiddleware` 同构：
     * 工具只暂存，真正的注入发生在**每一次模型调用之前**，注入后立刻清空。
     */
    private val pendingImages: com.xiaozhu.planner.core.tools.PendingImageStash =
        com.xiaozhu.planner.core.tools.PendingImageStash(),
) {

    fun run(history: MutableList<ChatMessage>): Flow<AgentEvent> = flow {
        for (round in 1..maxRounds) {
            val text = StringBuilder()
            val calls = mutableListOf<ToolCall>()
            var failure: String? = null

            // 工具调出来的旧图在这里注入（每一轮都查一次：模型可能在这一轮里才调 show_image，
            // 那次的结果要在**下一次**模型调用前出现）
            injectPendingImages(history)

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
            // continue_speaking 按"上一段有多少字"决定停多久，所以进工具前先记下来
            pacing.lastSegmentChars = text.length
            for (call in calls) {
                emit(AgentEvent.ToolStarted(call.function.name, call.function.arguments))
                val result = registry.execute(call)
                emit(AgentEvent.ToolFinished(call.function.name, result))
                history += ChatMessage.tool(call.id, call.function.name, result)
            }
        }
        emit(AgentEvent.Error("连续 $maxRounds 轮都在调用工具，已停下（可能陷入了循环）。"))
    }

    /**
     * 把 `show_image` 调出来的旧图作为一条 user 消息注入（含图片块）。
     *
     * 注入的图片**会留在上下文里**（与用户自己发的图一样），所以之后几轮它都还看得见；
     * 等压缩把它收进记忆节点时，又变回"引用 + 摘要里的一句话"。
     */
    private fun injectPendingImages(history: MutableList<ChatMessage>) {
        val taken = pendingImages.drain()
        if (taken.isEmpty()) return
        val urls = com.xiaozhu.planner.core.image.ImageStore.dataUrls(taken.map { it.first })
        if (urls.isEmpty()) return
        val whenText = taken.joinToString("、") { it.second }
        history += ChatMessage.userWithImages(
            "（这是你刚用 show_image 调出来的旧图，时间是 $whenText）",
            urls,
        )
    }
}