package com.xiaozhu.planner.core.llm

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 端点 URL 与请求头。**这里的每一条都对应一次真实的 404/400**：
 *
 * - 用户在「Base URL」里粘了完整端点（`.../v1/chat/completions`），
 *   客户端再拼一次 → `.../chat/completions/chat/completions` →
 *   网关返回的是**网站的 404 页面（HTML）**，看起来像"服务挂了"。
 * - opencode 的网关没有 `x-opencode-session` 会直接 400（MissingSessionID）。
 */
class EndpointUrlTest {

    @Test
    fun `裸的 base 拼一次`() {
        assertEquals(
            "https://opencode.ai/zen/go/v1/chat/completions",
            EndpointUrl.chatCompletions("https://opencode.ai/zen/go/v1"),
        )
    }

    @Test
    fun `粘了完整端点也不会拼两次（这次报的 404）`() {
        assertEquals(
            "https://opencode.ai/zen/go/v1/chat/completions",
            EndpointUrl.chatCompletions("https://opencode.ai/zen/go/v1/chat/completions"),
        )
    }

    @Test
    fun `尾部斜杠与空白都吃掉`() {
        val expect = "https://api.deepseek.com/v1/chat/completions"
        assertEquals(expect, EndpointUrl.chatCompletions("https://api.deepseek.com/v1/"))
        assertEquals(expect, EndpointUrl.chatCompletions("  https://api.deepseek.com/v1//  "))
        assertEquals(expect, EndpointUrl.chatCompletions("https://api.deepseek.com/v1/chat/completions/"))
    }

    @Test
    fun `带查询串的完整端点也能归一`() {
        assertEquals(
            "https://opencode.ai/zen/go/v1/chat/completions",
            EndpointUrl.chatCompletions("https://opencode.ai/zen/go/v1/chat/completions?foo=1"),
        )
    }

    @Test
    fun `normalize 出来的是根，且幂等`() {
        val root = EndpointUrl.normalize("https://opencode.ai/zen/go/v1/chat/completions/")
        assertEquals("https://opencode.ai/zen/go/v1", root)
        assertEquals(root, EndpointUrl.normalize(root))
        assertEquals(root, EndpointUrl.normalize(EndpointUrl.normalize(root)))
    }

    @Test
    fun `认得是不是 opencode 网关`() {
        assertTrue(EndpointUrl.isOpenCode("https://opencode.ai/zen/go/v1"))
        assertTrue(EndpointUrl.isOpenCode("https://opencode.ai/zen/go/v1/chat/completions"))
        assertTrue(EndpointUrl.isOpenCode("https://OPENCODE.AI/zen/go/v1"))
        assertFalse(EndpointUrl.isOpenCode("https://api.deepseek.com/v1"))
        assertFalse(EndpointUrl.isOpenCode("https://api.xiaomimimo.com/v1"))
    }

    @Test
    fun `session 头无条件发，不再按域名判断`() {
        // 一开始写成"只有 host 含 opencode.ai 才发"，一旦 base 写法/域名与预期不同
        // 头就静默不发，表现成"改了半天还是 400"
        assertEquals(
            mapOf("x-opencode-session" to "abc-123"),
            EndpointUrl.extraHeaders("https://api.deepseek.com/v1", "abc-123"),
        )
        assertEquals(
            mapOf("x-opencode-session" to "abc-123"),
            EndpointUrl.extraHeaders("https://whatever.example/v1", "abc-123"),
        )
        // 只有真没有 id 时才不发（发空值等于没发，还可能被判非法）
        assertEquals(emptyMap<String, String>(), EndpointUrl.extraHeaders("https://x/v1", ""))
        assertEquals(emptyMap<String, String>(), EndpointUrl.extraHeaders("https://x/v1", null))
    }

    @Test
    fun `报错能翻译成下一步该干什么`() {
        val missing = EndpointUrl.hintFor(400, """{"error":{"type":"MissingSessionID"}}""")
        assertTrue(missing != null && missing.contains("测试连接"))

        val html = EndpointUrl.hintFor(404, "<!DOCTYPE html><html lang=\"en\">")
        assertTrue(html != null && html.contains("Base URL"))

        assertTrue(EndpointUrl.hintFor(401, "unauthorized")!!.contains("API Key"))
        assertTrue(EndpointUrl.hintFor(429, "slow down")!!.contains("限流"))
        assertTrue(EndpointUrl.hintFor(503, "bad gateway")!!.contains("服务端"))
        // 没见过的错误不要硬编一段话
        assertEquals(null, EndpointUrl.hintFor(400, "something else"))
    }

    @Test
    fun `User-Agent 是自己的名字，不是通用库名`() {
        val ua = EndpointUrl.userAgent("0.1.0")
        assertEquals("xiaozhu-android/0.1.0", ua)
        // opencode 文档点名不要通用 SDK / HTTP 库名
        listOf("okhttp", "java", "curl", "python", "axios").forEach {
            assertFalse("UA 不该像通用库：$ua", ua.lowercase().contains(it))
        }
    }
}

/** 直接看 `buildRequest` 造出来的东西——不联网就能钉住"URL 与头拼得对不对"。 */
class OpenAiClientRequestTest {

    private val messages = listOf(ChatMessage.user("在吗"))

    private fun request(
        base: String,
        headers: Map<String, String> = emptyMap(),
    ) = OpenAiClient(base, "test-key", "deepseek-v4.1-flash", extraHeaders = headers)
        .buildRequest(messages, tools = null, stream = true)

    @Test
    fun `用户填的那种 base 不会拼成重复路径`() {
        val url = request("https://opencode.ai/zen/go/v1/chat/completions").url.toString()
        assertEquals("https://opencode.ai/zen/go/v1/chat/completions", url)
        assertFalse("重复路径就是这次 404 的原因", url.contains("chat/completions/chat"))
    }

    @Test
    fun `带上了认证头与流式 Accept`() {
        val req = request("https://opencode.ai/zen/go/v1")
        assertEquals("Bearer test-key", req.header("Authorization"))
        assertEquals("text/event-stream", req.header("Accept"))
        assertEquals("POST", req.method)
    }

    @Test
    fun `附加头会带上（opencode 的 session）`() {
        val req = request(
            "https://opencode.ai/zen/go/v1",
            EndpointUrl.extraHeaders("https://opencode.ai/zen/go/v1", "sess-9"),
        )
        assertEquals("sess-9", req.header("x-opencode-session"))
    }

    @Test
    fun `会自报 User-Agent，而不是默认的 okhttp`() {
        val req = OpenAiClient(
            "https://opencode.ai/zen/go/v1", "k", "m",
            userAgent = EndpointUrl.userAgent("0.1.0"),
        ).buildRequest(messages, tools = null, stream = true)
        assertEquals("xiaozhu-android/0.1.0", req.header("User-Agent"))
    }

    @Test
    fun `诊断用的头清单与实际发出的头一致`() {
        val client = OpenAiClient(
            "https://opencode.ai/zen/go/v1", "k", "m",
            extraHeaders = EndpointUrl.extraHeaders("https://opencode.ai/zen/go/v1", "s"),
            userAgent = EndpointUrl.userAgent("0.1.0"),
        )
        val req = client.buildRequest(messages, tools = null, stream = true)
        // 设置页的「测试连接」就是拿这个清单告诉用户"到底发了什么"，
        // 对不上就会把人带沟里
        client.sentHeaders().forEach { name ->
            assertTrue("$name 声称会发但实际没发", req.header(name) != null)
        }
        assertEquals("https://opencode.ai/zen/go/v1/chat/completions", client.endpoint())
    }

    @Test
    fun `请求体里是模型名与 stream 标记`() {
        val body = request("https://opencode.ai/zen/go/v1").body!!
        val text = okio.Buffer().also { body.writeTo(it) }.readUtf8()
        assertTrue(text, text.contains("\"model\":\"deepseek-v4.1-flash\""))
        assertTrue("流式必须显式带 stream=true，否则端点不走 SSE", text.contains("\"stream\":true"))
        assertTrue(text.contains("\"stream_options\""))
    }
}
