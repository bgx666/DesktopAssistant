package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * 直连 OpenAI 兼容端点（默认 DeepSeek）的流式客户端。
 *
 * 只依赖 OkHttp，不用任何厂商 SDK——协议就是 `POST {base}/chat/completions` + SSE。
 * 取消：协程被取消时 [okhttp3.Call.cancel] 会被调用，不会留下挂着的连接。
 */
class OpenAiClient(
    private val baseUrl: String,
    private val apiKey: String,
    private val model: String,
    private val client: OkHttpClient = defaultClient(),
) : LlmStreamer {

    override fun stream(
        messages: List<ChatMessage>,
        tools: List<ToolSpec>,
    ): Flow<LlmEvent> = flow {
        val url = baseUrl.trimEnd('/') + "/chat/completions"
        val body = PlannerJson.encodeToString(
            ChatRequest.serializer(),
            ChatRequest(model = model, messages = messages, tools = tools.ifEmpty { null }),
        )
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Accept", "text/event-stream")
            .post(body.toRequestBody(JSON_MEDIA))
            .build()

        val call = client.newCall(request)
        try {
            call.execute().use { response ->
                if (!response.isSuccessful) {
                    val detail = try {
                        response.body?.string()?.take(500).orEmpty()
                    } catch (_: Exception) {
                        ""
                    }
                    emit(LlmEvent.Failed("HTTP ${response.code} ${response.message} $detail".trim()))
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
    @SerialName("stream_options") val streamOptions: StreamOptions = StreamOptions(),
)

@Serializable
internal data class StreamOptions(@SerialName("include_usage") val includeUsage: Boolean = true)