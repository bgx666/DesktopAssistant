package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * 直连 OpenAI 兼容端点（默认 DeepSeek）。
 *
 * - [stream]：对话用，SSE 流式 + tool calls；取消即断开连接
 * - [complete]：压缩摘要用，一次性返回正文（不进对话上下文）
 */
class OpenAiClient(
    private val baseUrl: String,
    private val apiKey: String,
    private val model: String,
    private val client: OkHttpClient = defaultClient(),
) : LlmStreamer, LlmCompleter {

    override fun stream(
        messages: List<ChatMessage>,
        tools: List<ToolSpec>,
    ): Flow<LlmEvent> = flow {
        val request = buildRequest(messages, tools.ifEmpty { null }, stream = true)
        val call = client.newCall(request)
        try {
            call.execute().use { response ->
                if (!response.isSuccessful) {
                    emit(LlmEvent.Failed("HTTP ${response.code} ${response.message} ${errorBody(response)}".trim()))
                    return@flow
                }
                val source = response.body?.source()
                if (source == null) {
                    emit(LlmEvent.Failed("响应没有 body"))
                    return@flow
                }
                val parser = ChatStreamParser()
                while (true) {
                    val line = source.readUtf8Line() ?: break
                    for (delta in parser.feedLine(line)) emit(delta.toEvent())
                }
                for (delta in parser.finish()) emit(delta.toEvent())
            }
        } catch (e: Exception) {
            emit(LlmEvent.Failed("${e.javaClass.simpleName}: ${e.message ?: "请求失败"}"))
        } finally {
            call.cancel()
        }
    }.flowOn(Dispatchers.IO)

    override suspend fun complete(messages: List<ChatMessage>): String = withContext(Dispatchers.IO) {
        val request = buildRequest(messages, null, stream = false)
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IllegalStateException("HTTP ${response.code} ${errorBody(response)}".trim())
            }
            val text = response.body?.string().orEmpty()
            val parsed = PlannerJson.decodeFromString(ChatResponse.serializer(), text)
            parsed.choices.firstOrNull()?.message?.content.orEmpty()
        }
    }

    private fun buildRequest(
        messages: List<ChatMessage>,
        tools: List<ToolSpec>?,
        stream: Boolean,
    ): Request {
        val body = PlannerJson.encodeToString(
            ChatRequest.serializer(),
            ChatRequest(
                model = model,
                messages = messages,
                tools = tools,
                stream = stream,
                streamOptions = if (stream) StreamOptions() else null,
            ),
        )
        return Request.Builder()
            .url(baseUrl.trimEnd('/') + "/chat/completions")
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Accept", if (stream) "text/event-stream" else "application/json")
            .post(body.toRequestBody(JSON_MEDIA))
            .build()
    }

    private fun errorBody(response: okhttp3.Response): String = try {
        response.body?.string()?.take(500).orEmpty()
    } catch (_: Exception) {
        ""
    }

    private fun StreamDelta.toEvent(): LlmEvent = when (this) {
        is StreamDelta.Content -> LlmEvent.TextDelta(text)
        is StreamDelta.ToolCalls -> LlmEvent.ToolCalls(calls)
        is StreamDelta.Done -> LlmEvent.Finished(reason)
    }

    companion object {
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS) // 流式：不设读超时，靠协程取消
            .writeTimeout(60, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }
}

@Serializable
internal data class ChatRequest(
    val model: String,
    val messages: List<ChatMessage>,
    val tools: List<ToolSpec>? = null,
    val stream: Boolean = true,
    @SerialName("stream_options") val streamOptions: StreamOptions? = null,
)

@Serializable
internal data class StreamOptions(@SerialName("include_usage") val includeUsage: Boolean = true)