package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * 拿**真实网关**验一遍 App 自己的客户端代码（live check）。
 *
 * 为什么要它：单测只能证明"我按理解拼了请求"，证明不了"这个请求网关收不收"。
 * 真实踩到的两次故障都属于后者——404（路径重复）和
 * `400 {"model":"deepseek-v4.1-flash"}`（messages 为空），
 * 两者的表现都不是"拼错了"，而是**服务端用一种看不懂的方式拒绝**。
 *
 * 默认跳过（不在 CI 里联网、也不把 key 写进仓库）。本机要验：
 *
 * ```powershell
 * $env:PLANNER_LIVE_KEY = "<你的 key>"
 * $env:PLANNER_LIVE_BASE = "https://opencode.ai/zen/go/v1"   # 可选
 * cd android; powershell -File build.ps1 -Task test
 * ```
 */
class LiveGatewayTest {

    private val key = System.getenv("PLANNER_LIVE_KEY").orEmpty()
    private val base = System.getenv("PLANNER_LIVE_BASE").orEmpty()
        .ifBlank { "https://opencode.ai/zen/go/v1" }
    private val model = System.getenv("PLANNER_LIVE_MODEL").orEmpty()
        .ifBlank { "deepseek-v4.1-flash" }

    private fun client(extraHeaders: Map<String, String> = EndpointUrl.extraHeaders(base, "live-test-session")) =
        OpenAiClient(
            baseUrl = base,
            apiKey = key,
            model = model,
            extraHeaders = extraHeaders,
            userAgent = EndpointUrl.userAgent("live-check"),
        )

    @Test
    fun `真实的 system+user 一轮能拿到回复`() = runBlocking {
        assumeTrue("没设 PLANNER_LIVE_KEY，跳过联网检查", key.isNotBlank())

        val reply = withTimeout(60_000) {
            client().complete(
                listOf(
                    ChatMessage.system("你是小助，用中文回答。"),
                    ChatMessage.user("只回两个字：收到"),
                )
            )
        }
        println("[live] 回复=${reply.take(80)}")
        assertTrue("网关收了请求但没给正文：'$reply'", reply.isNotBlank())
    }

    @Test
    fun `流式一轮能收到增量与结束`() = runBlocking {
        assumeTrue("没设 PLANNER_LIVE_KEY，跳过联网检查", key.isNotBlank())

        val events = withTimeout(60_000) {
            client().stream(
                listOf(
                    ChatMessage.system("你是小助。"),
                    ChatMessage.user("数到三"),
                ),
                emptyList(),
            ).let { flow ->
                val out = mutableListOf<LlmEvent>()
                flow.collect { out += it }
                out
            }
        }
        val text = events.filterIsInstance<LlmEvent.TextDelta>().joinToString("") { it.text }
        val failed = events.filterIsInstance<LlmEvent.Failed>().firstOrNull()
        println("[live] 流式文本=${text.take(80)}  事件数=${events.size}")
        assertTrue("流式失败：${failed?.message}", failed == null)
        assertTrue("没有任何增量也没有结束事件", events.isNotEmpty())
    }

    @Test
    fun `带工具的一轮也能过（请求体最复杂的那种）`() = runBlocking {
        assumeTrue("没设 PLANNER_LIVE_KEY，跳过联网检查", key.isNotBlank())

        val tools = listOf(
            ToolSpec(
                function = FunctionSpec(
                    name = "heartbeat",
                    description = "设定下次醒来的时间",
                    parameters = buildJsonObject {
                        put("type", "object")
                        putJsonObject("properties") {
                            putJsonObject("minutes") { put("type", "number") }
                        }
                    },
                )
            )
        )
        val events = withTimeout(60_000) {
            val out = mutableListOf<LlmEvent>()
            client().stream(
                listOf(
                    ChatMessage.system("你是小助。"),
                    ChatMessage.user("半小时后提醒我喝水"),
                ),
                tools,
            ).collect { out += it }
            out
        }
        val failed = events.filterIsInstance<LlmEvent.Failed>().firstOrNull()
        println("[live] 带工具事件=${events.map { it::class.simpleName }}")
        assertFalse("带工具的请求被拒：${failed?.message}", failed != null)
    }

    @Test
    fun `没有 session 头会被网关拒（证明这个头是必须的）`() = runBlocking {
        assumeTrue("没设 PLANNER_LIVE_KEY，跳过联网检查", key.isNotBlank())
        assumeTrue("只对 opencode 网关有意义", EndpointUrl.isOpenCode(base))

        val error = runCatching {
            withTimeout(60_000) {
                client(extraHeaders = emptyMap()).complete(listOf(ChatMessage.user("hi")))
            }
        }.exceptionOrNull()

        println("[live] 不带 session 头 → ${error?.message?.take(200)}")
        assertTrue("网关竟然收了没有 session 头的请求？", error != null)
        assertTrue(
            "预期是 MissingSessionID，实际：${error?.message}",
            error!!.message!!.contains("MissingSessionID", ignoreCase = true),
        )
    }
}
