package com.xiaozhu.planner.core.llm

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** 解析器吐出的原始增量。 */
internal sealed interface StreamDelta {
    data class Content(val text: String) : StreamDelta
    data class ToolCalls(val calls: List<ToolCall>) : StreamDelta
    data class Done(val reason: String?) : StreamDelta
}

/**
 * OpenAI 兼容 SSE 流的**逐行**解析器（纯 Kotlin，可 JVM 单测）。
 *
 * 为什么单独拆出来：tool_calls 在流里是**碎片**——第一个碎片带 id 和函数名、参数被切成很多段、
 * 多个工具并行时靠 `index` 区分。这段拼装是最容易写错的地方，所以做成无副作用、可测的类。
 *
 * 用法：`source.readUtf8Line()` 逐行喂给 [feedLine]，流结束时调 [finish]。
 */
internal class ChatStreamParser {

    private class Pending {
        var id: String = ""
        var name: String = ""
        val args = StringBuilder()
    }

    private val pending = LinkedHashMap<Int, Pending>()
    private var sawDone = false
    private var lastReason: String? = null

    /** 喂一行（不含换行符）。返回可立即消费的增量。 */
    fun feedLine(line: String): List<StreamDelta> {
        val trimmed = line.trim()
        // SSE：空行是分隔符；以 ":" 开头是注释；其余字段（event:/id:/retry:）本项目不用
        if (trimmed.isEmpty() || trimmed.startsWith(":")) return emptyList()
        if (!trimmed.startsWith(DATA_PREFIX)) return emptyList()

        val payload = trimmed.substring(DATA_PREFIX.length).trim()
        if (payload == DONE) {
            sawDone = true
            return buildList {
                toolCallsOrNull()?.let { add(it) }
                add(StreamDelta.Done(lastReason))
            }
        }

        val chunk = try {
            PlannerJson.decodeFromString(Chunk.serializer(), payload)
        } catch (_: Exception) {
            return emptyList() // 半截 JSON / 未知事件：忽略，不让它打断整轮
        }

        val out = mutableListOf<StreamDelta>()
        val choice = chunk.choices.firstOrNull()
        choice?.delta?.content?.let { if (it.isNotEmpty()) out.add(StreamDelta.Content(it)) }
        choice?.delta?.toolCalls?.forEach { delta ->
            val slot = pending.getOrPut(delta.index) { Pending() }
            delta.id?.let { if (it.isNotEmpty()) slot.id = it }
            delta.function?.name?.let { if (it.isNotEmpty()) slot.name = it }
            delta.function?.arguments?.let { slot.args.append(it) }
        }
        choice?.finishReason?.let { reason ->
            lastReason = reason
            // 有的服务端不发 [DONE]，只给 finish_reason —— 这里就把工具交出去
            if (reason == "tool_calls") toolCallsOrNull()?.let { out.add(it) }
        }
        return out
    }

    /** 流结束（正常读完或对端断开）。返回剩余增量；多次调用只有第一次有效。 */
    fun finish(): List<StreamDelta> {
        if (sawDone) return emptyList()
        sawDone = true
        return buildList {
            toolCallsOrNull()?.let { add(it) }
            add(StreamDelta.Done(lastReason))
        }
    }

    private fun toolCallsOrNull(): StreamDelta.ToolCalls? {
        if (pending.isEmpty()) return null
        val calls = pending.entries
            .sortedBy { it.key }
            .map { (_, slot) ->
                ToolCall(
                    id = slot.id,
                    function = ToolCallFunction(
                        name = slot.name,
                        arguments = slot.args.toString().ifBlank { "{}" },
                    ),
                )
            }
        pending.clear()
        return StreamDelta.ToolCalls(calls)
    }

    private companion object {
        const val DATA_PREFIX = "data:"
        const val DONE = "[DONE]"
    }
}

// ── 线格式（只声明用得到的字段，其余靠 ignoreUnknownKeys 丢掉）──

@Serializable
internal data class Chunk(val choices: List<ChunkChoice> = emptyList())

@Serializable
internal data class ChunkChoice(
    val delta: ChunkDelta? = null,
    @SerialName("finish_reason") val finishReason: String? = null,
)

@Serializable
internal data class ChunkDelta(
    val content: String? = null,
    @SerialName("tool_calls") val toolCalls: List<ChunkToolCall>? = null,
)

@Serializable
internal data class ChunkToolCall(
    val index: Int = 0,
    val id: String? = null,
    val function: ChunkFunction? = null,
)

@Serializable
internal data class ChunkFunction(
    val name: String? = null,
    val arguments: String? = null,
)