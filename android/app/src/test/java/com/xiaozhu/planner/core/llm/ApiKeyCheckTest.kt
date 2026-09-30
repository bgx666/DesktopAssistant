package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 密钥体检。
 *
 * 起因是一次真实报错：用户粘贴密钥时把说明文字也带进去了，请求头变成
 * `Authorization: Bearer 你的...`，OkHttp 抛的是
 * `Unexpected char 0x4f60 at 7 in Authorization value`——
 * **0x4f60 是「你」的编码，索引 7 是密钥第一个字符**，这话对用户毫无意义。
 */
class ApiKeyCheckTest {

    @Test
    fun `清洗掉粘贴常带的垃圾`() {
        assertEquals("oc_s-abc123", ApiKeyCheck.sanitize("  oc_s-abc123  "))
        assertEquals("oc_s-abc123", ApiKeyCheck.sanitize("\"oc_s-abc123\""))
        assertEquals("oc_s-abc123", ApiKeyCheck.sanitize("“oc_s-abc123”"))
        assertEquals("oc_s-abc123", ApiKeyCheck.sanitize("Bearer oc_s-abc123"))
        assertEquals("oc_s-abc123", ApiKeyCheck.sanitize("bearer oc_s-abc123"))
        // 换行会让整个头非法
        assertEquals("oc_s-abc123", ApiKeyCheck.sanitize("oc_s-abc\n123".replace("\n", "")))
        assertEquals("oc_sabc123", ApiKeyCheck.sanitize("oc_s\nabc\r123"))
    }

    @Test
    fun `认出中文并指出是第几个字符（就是用户遇到的那个）`() {
        val key = "你的密钥"
        val problem = ApiKeyCheck.problem(key)!!
        assertTrue("要说清是第 1 个字符：$problem", problem.contains("第 1 个字符"))
        assertTrue("要点出那个字：$problem", problem.contains("你"))
        assertTrue("要告诉怎么办：$problem", problem.contains("清空"))
    }

    @Test
    fun `中文夹在中间也能定位`() {
        val problem = ApiKeyCheck.problem("sk-abc你def")!!
        assertTrue(problem.contains("第 7 个字符"))
    }

    @Test
    fun `空格与过短的密钥都提示`() {
        assertTrue(ApiKeyCheck.problem("sk-abc def")!!.contains("空格"))
        assertTrue(ApiKeyCheck.problem("sk-123")!!.contains("不像完整"))
        assertTrue(ApiKeyCheck.problem("")!!.contains("还没填"))
    }

    @Test
    fun `正常的密钥不报问题`() {
        assertNull(ApiKeyCheck.problem("oc_s-1234567890abcdefghijklmnop"))
        assertNull(ApiKeyCheck.problem("sk-0123456789abcdef0123456789abcdef"))
    }

    @Test
    fun `带 Bearer 前缀的密钥在清洗后就正常了`() {
        // 设置时会被 sanitize 掉，所以再体检就没问题
        assertNull(ApiKeyCheck.problem(ApiKeyCheck.sanitize("Bearer sk-0123456789abcdef0123456789ab")))
    }

    @Test
    fun `密钥有问题时客户端不发请求（直接给可读的错误）`() = runBlocking {
        val client = OpenAiClient("https://opencode.ai/zen/go/v1", "你的密钥", "m")
        val failed = client.stream(listOf(ChatMessage.user("hi")), emptyList())
            .toList()
            .filterIsInstance<LlmEvent.Failed>()
            .firstOrNull()

        assertTrue("应该本地拦下", failed != null)
        assertTrue(
            "错误要说人话，而不是 OkHttp 的 0x4f60：${failed!!.message}",
            failed.message.contains("API Key") && !failed.message.contains("0x4f60"),
        )

        val error = runCatching { client.complete(listOf(ChatMessage.user("hi"))) }.exceptionOrNull()
        assertTrue(error != null && error.message!!.contains("API Key"))
    }
}
