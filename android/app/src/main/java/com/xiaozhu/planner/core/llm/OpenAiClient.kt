package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * 直连 OpenAI 兼容端点（默认 opencode 网关 / DeepSeek）。
 *
 * - [stream]：对话用，SSE 流式 + tool calls；取消即断开连接
 * - [complete]：压缩摘要用，一次性返回正文（不进对话上下文）
 *
 * [baseUrl] 带不带 `/chat/completions` 都行（见 [EndpointUrl]）；
 * [extraHeaders] 放服务商额外要求的头（如 opencode 的 session）；
 * [userAgent] 必须自报家门（网关会拒绝通用库名，见 [EndpointUrl.userAgent]）。
 */
class OpenAiClient(
    private val baseUrl: String,
    private val apiKey: String,
    private val model: String,
    private val client: OkHttpClient = defaultClient(),
    private val extraHeaders: Map<String, String> = emptyMap(),
    private val userAgent: String = EndpointUrl.userAgent("dev"),
    /** 思考开关与强度（设置页可改；见 [ThinkingControl]）。 */
    private val thinkingMode: String = ThinkingControl.ADAPTIVE,
    private val reasoningEffort: String = ReasoningEffort.HIGH,
) : LlmStreamer, LlmCompleter {

    /** 这个客户端会发哪些头（设置页的「测试连接」用它把实际情况摆出来）。 */
    fun sentHeaders(): List<String> =
        listOf("Authorization", "Accept", USER_AGENT_HEADER) + extraHeaders.keys

    /** 实际会 POST 的地址（诊断用）。 */
    fun endpoint(): String = EndpointUrl.chatCompletions(baseUrl)

    override fun stream(
        messages: List<ChatMessage>,
        tools: List<ToolSpec>,
    ): Flow<LlmEvent> = flow {
        // 空 messages 会被网关回一句 `{"model":"..."}` 的 400——那句报错完全看不出病因。
        // 在这里拦下，换成人能看懂的一句话（真实踩过：排查了很久才发现是"没发消息"）。
        if (messages.isEmpty()) {
            emit(LlmEvent.Failed("这一轮没有可发送的消息（内部状态为空），已拦下未发请求。"))
            return@flow
        }
        // 密钥有问题时**不要发请求**：HTTP 头只能放 ASCII，中文会让 OkHttp 抛
        // `Unexpected char 0x4f60 at 7 in Authorization value` 这种天书
        ApiKeyCheck.problem(apiKey)?.let { problem ->
            emit(LlmEvent.Failed(problem))
            return@flow
        }
        val request = buildRequest(messages, tools.ifEmpty { null }, stream = true)
        val call = client.newCall(request)
        try {
            call.execute().use { response ->
                if (!response.isSuccessful) {
                    emit(LlmEvent.Failed(describeFailure(response, describeRequest(messages, tools.size, true))))
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
        require(messages.isNotEmpty()) { "这一轮没有可发送的消息（内部状态为空），已拦下未发请求。" }
        ApiKeyCheck.problem(apiKey)?.let { throw IllegalStateException(it) }
        val request = buildRequest(messages, null, stream = false)
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw IllegalStateException(
                    describeFailure(response, describeRequest(messages, 0, false))
                )
            }
            val text = response.body?.string().orEmpty()
            val parsed = PlannerJson.decodeFromString(ChatResponse.serializer(), text)
            parsed.choices.firstOrNull()?.message?.content.orEmpty()
        }
    }

    /**
     * 报错要能直接指导下一步（见 [EndpointUrl.hintFor]），并且带上**这次发了什么**。
     *
     * 为什么要把请求形态也写进去：网关的报错经常完全看不出病因
     * （`messages` 为空时只回一句 `{"model":"..."}`），没有这一行就只能靠猜。
     */
    private fun describeFailure(response: okhttp3.Response, requestInfo: String): String {
        val body = errorBody(response)
        val head = "HTTP ${response.code} ${response.message}".trim()
        // 报错路径**绝不允许再抛异常**：hint 只是锦上添花，为它崩掉就把
        // "看得见的 400" 变成了"看不懂的崩溃"（真机上真发生过一次）
        val hint = runCatching { EndpointUrl.hintFor(response.code, body) }.getOrNull()
        return listOfNotNull(
            head,
            "本次请求：$requestInfo",
            hint?.let { "→ $it" },
            body.take(300).takeIf { it.isNotBlank() },
        ).joinToString("\n")
    }

    /** 请求形态摘要（消息条数、工具数、正文大小），用于报错与诊断。 */
    private fun describeRequest(messages: List<ChatMessage>, toolCount: Int, stream: Boolean): String {
        val bytes = PlannerJson.encodeToString(
            ChatRequest.serializer(),
            ChatRequest(
                model = model,
                messages = messages.map { it.toWire() },
                stream = stream,
                thinking = thinkingPayload()?.get("thinking") as? kotlinx.serialization.json.JsonObject,
                reasoningEffort = thinkingPayload()
                    ?.get("reasoning_effort")
                    ?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content },
            ),
        ).length
        return buildString {
            append("messages=").append(messages.size)
            if (toolCount > 0) append("、tools=").append(toolCount)
            append("、约 ").append(bytes).append(" 字节")
            if (stream) append("、stream=true")
        }
    }

    /** 思考参数（设置页的三选一 + 强度）；非 DeepSeek 模型返回 null。 */
    private fun thinkingPayload(): kotlinx.serialization.json.JsonObject? =
        ThinkingControl.payload(model, thinkingMode, reasoningEffort)

    /** 内部可见是为了能单测"URL 与请求头拼得对不对"——这次的 404 就是拼错了。 */
    internal fun buildRequest(
        messages: List<ChatMessage>,
        tools: List<ToolSpec>?,
        stream: Boolean,
    ): Request {
        // 思考开关与强度来自设置（见 ThinkingControl）；非 DeepSeek 模型整块不发出
        val thinking = thinkingPayload()
        val body = PlannerJson.encodeToString(
            ChatRequest.serializer(),
            ChatRequest(
                model = model,
                messages = messages.map { it.toWire() },
                tools = tools,
                stream = stream,
                streamOptions = if (stream) StreamOptions() else null,
                thinking = thinking?.get("thinking") as? kotlinx.serialization.json.JsonObject,
                reasoningEffort = thinking
                    ?.get("reasoning_effort")
                    ?.let { (it as? kotlinx.serialization.json.JsonPrimitive)?.content },
            ),
        )
        val builder = Request.Builder()
            .url(EndpointUrl.chatCompletions(baseUrl))
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Accept", if (stream) "text/event-stream" else "application/json")
            .addHeader(USER_AGENT_HEADER, userAgent)
        extraHeaders.forEach { (name, value) -> builder.addHeader(name, value) }
        return builder.post(body.toRequestBody(JSON_MEDIA)).build()
    }

    /**
     * 把内部消息转成协议形状。
     *
     * **有图时 `content` 必须是数组**（`[{"type":"text",...},{"type":"image_url",...}]`），
     * 纯文本时仍是字符串——这是 OpenAI 协议的规定，写错端点会 400。
     * 转换只在这一处做，别的地方都按"文本 + 图"的简单形状处理。
     */
    private fun ChatMessage.toWire(): ChatMessageWire = ChatMessageWire(
        role = role,
        content = if (images.isEmpty()) {
            content?.let { kotlinx.serialization.json.JsonPrimitive(it) }
        } else {
            buildJsonArray {
                if (!content.isNullOrBlank()) {
                    add(buildJsonObject { put("type", "text"); put("text", content) })
                }
                images.forEach { url ->
                    add(
                        buildJsonObject {
                            put("type", "image_url")
                            putJsonObject("image_url") { put("url", url) }
                        }
                    )
                }
            }
        },
        name = name,
        toolCalls = toolCalls,
        toolCallId = toolCallId,
    )

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

        /** OkHttp 默认也会发 User-Agent，但我们显式覆盖它（见 [EndpointUrl.userAgent]）。 */
        const val USER_AGENT_HEADER = "User-Agent"

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
    val messages: List<ChatMessageWire>,
    val tools: List<ToolSpec>? = null,
    val stream: Boolean = true,
    @SerialName("stream_options") val streamOptions: StreamOptions? = null,
    /**
     * 思考开关：`{"type":"adaptive|enabled|disabled"}`（官方字段，见 [ThinkingControl]）。
     * 非 DeepSeek 模型为 null → 不发出。
     */
    val thinking: kotlinx.serialization.json.JsonObject? = null,
    /** 思考强度 low/high/max；`thinking.type=disabled` 时**不发**（官方会忽略）。 */
    @SerialName("reasoning_effort") val reasoningEffort: String? = null,
)

/**
 * 真正发出去的报文形状：`content` 可能是字符串（纯文本），也可能是块数组（带图）。
 *
 * 用 [JsonElement] 而不是 String —— 只有这样才能在同一字段里表达两种形状，
 * 而不用给 ChatMessage 写自定义序列化器（那会把"文本 + 图"的简单模型搞复杂）。
 */
@Serializable
internal data class ChatMessageWire(
    val role: String,
    val content: kotlinx.serialization.json.JsonElement? = null,
    val name: String? = null,
    @SerialName("tool_calls") val toolCalls: List<ToolCall>? = null,
    @SerialName("tool_call_id") val toolCallId: String? = null,
)

@Serializable
internal data class StreamOptions(@SerialName("include_usage") val includeUsage: Boolean = true)