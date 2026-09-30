package com.xiaozhu.planner.core.llm

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/** 全局 JSON 配置：容忍未知字段（服务端随时可能加），请求时省略默认值。 */
val PlannerJson: Json = Json {
    ignoreUnknownKeys = true
    isLenient = true
    explicitNulls = false
    // encodeDefaults = true 是必须的：否则 stream=true 与 stream_options 会被当默认值省略，
    // 端点就不走 SSE，解析器什么都收不到。explicitNulls=false 保证 null 字段仍然不发出。
    encodeDefaults = true
}

/**
 * 一条对话消息（OpenAI /chat/completions 的形状）。
 *
 * [images] 是 data URL（`data:image/jpeg;base64,...`）。**有图时 content 会变成数组**
 * （`[{"type":"text",...},{"type":"image_url",...}]`），这是 OpenAI 协议的硬要求；
 * 转换放在 [OpenAiClient] 里做，这一层保持"文本 + 图"的简单形状。
 */
@Serializable
data class ChatMessage(
    val role: String,
    val content: String? = null,
    val name: String? = null,
    @SerialName("tool_calls") val toolCalls: List<ToolCall>? = null,
    @SerialName("tool_call_id") val toolCallId: String? = null,
    val images: List<String> = emptyList(),
) {
    companion object {
        fun system(text: String) = ChatMessage(role = "system", content = text)
        fun user(text: String) = ChatMessage(role = "user", content = text)

        /** 带图的一条用户消息。 */
        fun userWithImages(text: String, images: List<String>) =
            ChatMessage(role = "user", content = text, images = images)

        fun assistant(text: String) = ChatMessage(role = "assistant", content = text)
        fun assistantToolCalls(calls: List<ToolCall>, text: String = "") =
            ChatMessage(role = "assistant", content = text, toolCalls = calls)
        fun tool(callId: String, name: String, text: String) =
            ChatMessage(role = "tool", content = text, toolCallId = callId, name = name)
    }
}

/** 模型要求调用某个工具。 */
@Serializable
data class ToolCall(
    val id: String,
    val type: String = "function",
    val function: ToolCallFunction,
)

@Serializable
data class ToolCallFunction(
    val name: String,
    /** JSON 字符串（不是对象）——和 OpenAI 协议一致。 */
    val arguments: String = "{}",
)

/** 暴露给模型的工具声明。 */
@Serializable
data class ToolSpec(
    val type: String = "function",
    val function: FunctionSpec,
)

@Serializable
data class FunctionSpec(
    val name: String,
    val description: String,
    val parameters: JsonObject,
)

/** 流式事件：UI / agent 循环消费这个。 */
sealed interface LlmEvent {
    /** 一段正文增量（直接追加到气泡）。 */
    data class TextDelta(val text: String) : LlmEvent

    /** 本轮模型要调用的工具（已拼装完整）。 */
    data class ToolCalls(val calls: List<ToolCall>) : LlmEvent

    /** 本轮结束（reason 通常是 stop / tool_calls）。 */
    data class Finished(val reason: String?) : LlmEvent

    /** 失败（网络/HTTP/协议）。 */
    data class Failed(val message: String) : LlmEvent
}