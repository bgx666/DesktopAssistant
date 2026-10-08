package com.xiaozhu.planner.core.llm

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 思考开关与强度（官方 `thinking.type` + `reasoning_effort`）。
 *
 * 实测依据（2026-09-30，DeepSeek 官方）：
 * - `thinking.type` 只接受 `adaptive` / `enabled` / `disabled`（传 `none` 会 422）
 * - `disabled` 时 **reasoning token 真的是 0**，同一道题快约 30%
 * - `disabled` 时 `reasoning_effort` **会被忽略**（`disabled + effort=high` 仍是 0 推理）
 *   → 所以关掉思考就不该再发它，界面上也该把强度藏起来
 */
class ThinkingControlTest {

    private fun payloadJson(model: String, mode: String, effort: String): String? =
        ThinkingControl.payload(model, mode, effort)?.toString()

    @Test
    fun `自适应是默认行为，带默认强度`() {
        val json = payloadJson("deepseek-flash", ThinkingControl.ADAPTIVE, ReasoningEffort.HIGH)!!
        assertTrue(json.contains("\"type\":\"adaptive\""))
        assertTrue(json.contains("\"reasoning_effort\":\"high\""))
    }

    @Test
    fun `关闭思考时不发强度（官方会忽略它）`() {
        val payload = ThinkingControl.payload("deepseek-flash", ThinkingControl.DISABLED, ReasoningEffort.MAX)!!
        assertEquals(
            "disabled",
            (payload["thinking"]?.jsonObject?.get("type") as? JsonPrimitive)?.content,
        )
        assertFalse(
            "关闭时不该发 reasoning_effort：发了只会让人以为强度还在起作用",
            payload.containsKey("reasoning_effort"),
        )
    }

    @Test
    fun `总是思考时强度照发`() {
        val payload = ThinkingControl.payload("deepseek-flash", ThinkingControl.ENABLED, ReasoningEffort.LOW)!!
        assertEquals("enabled", (payload["thinking"]?.jsonObject?.get("type") as? JsonPrimitive)?.content)
        assertEquals("low", (payload["reasoning_effort"] as? JsonPrimitive)?.content)
    }

    @Test
    fun `官方现名与旧别名都要认`() {
        // 官方现在叫 deepseek-flash（正式名 DeepSeek-V4.1-Flash），
        // 前缀判断只认 deepseek-v4 的话，用官方名时设置会静默失效
        listOf("deepseek-flash", "DeepSeek-Flash", " deepseek-flash ", "deepseek-v4-flash-vision-exp")
            .forEach { model ->
                assertTrue("$model 应该支持思考参数", ThinkingControl.supports(model))
            }
    }

    @Test
    fun `别的服务商一个字段都不发`() {
        assertNull(ThinkingControl.payload("gpt-4o", ThinkingControl.ENABLED, ReasoningEffort.MAX))
        assertNull(ThinkingControl.payload("qwen-max", ThinkingControl.ADAPTIVE, ReasoningEffort.HIGH))
        assertNull(ThinkingControl.payload("", ThinkingControl.ENABLED, ReasoningEffort.MAX))
    }

    @Test
    fun `脏值回落到默认，不会传到线上`() {
        val payload = ThinkingControl.payload("deepseek-flash", "乱七八糟", "离谱")!!
        assertEquals("adaptive", (payload["thinking"]?.jsonObject?.get("type") as? JsonPrimitive)?.content)
        assertEquals("high", (payload["reasoning_effort"] as? JsonPrimitive)?.content)
    }

    @Test
    fun `三档与三种模式都有界面文案`() {
        ThinkingControl.MODES.forEach { assertTrue(ThinkingControl.modeLabel(it).isNotBlank()) }
        ThinkingControl.MODES.forEach { assertTrue(ThinkingControl.modeHint(it).isNotBlank()) }
        ThinkingControl.EFFORTS.forEach { assertTrue(ThinkingControl.effortLabel(it).isNotBlank()) }
        assertEquals(3, ThinkingControl.MODES.size)
        assertEquals(3, ThinkingControl.EFFORTS.size)
    }

    @Test
    fun `请求体里真的带上了 thinking（走完整 buildRequest）`() {
        val client = OpenAiClient(
            baseUrl = "https://api.deepseek.com/v1",
            apiKey = "sk-0123456789abcdef",
            model = "deepseek-flash",
            thinkingMode = ThinkingControl.DISABLED,
            reasoningEffort = ReasoningEffort.MAX,
        )
        val request = client.buildRequest(listOf(ChatMessage.user("你好")), null, stream = false)
        val buffer = okio.Buffer()
        request.body!!.writeTo(buffer)
        val body = buffer.readUtf8()

        assertTrue("要带 thinking：$body", body.contains("\"thinking\":{\"type\":\"disabled\"}"))
        assertFalse("关闭思考时不该带 reasoning_effort：$body", body.contains("reasoning_effort"))
    }

    @Test
    fun `开启思考时请求体带上模式与强度`() {
        val client = OpenAiClient(
            baseUrl = "https://api.deepseek.com/v1",
            apiKey = "sk-0123456789abcdef",
            model = "deepseek-flash",
            thinkingMode = ThinkingControl.ENABLED,
            reasoningEffort = ReasoningEffort.MAX,
        )
        val request = client.buildRequest(listOf(ChatMessage.user("你好")), null, stream = false)
        val buffer = okio.Buffer()
        request.body!!.writeTo(buffer)
        val body = buffer.readUtf8()

        assertTrue(body.contains("\"type\":\"enabled\""))
        assertTrue(body.contains("\"reasoning_effort\":\"max\""))
    }
}
