package com.xiaozhu.planner.core.llm

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 推理档位。
 *
 * 档位给 `high`（官方默认）：用一道真实的方案题实测，`low` 反而推理更多、
 * 答得更短、还更慢（low 3005/1604字/20.5s vs high 1645/2222字/15.4s）。
 * 只发 effort，不发 `thinking`——默认就在推理，显式打开没收益。
 */
class ReasoningEffortTest {

    private fun bodyOf(model: String): String {
        val client = OpenAiClient("https://api.deepseek.com/v1", "sk-0123456789abcdef", model)
        val request = client.buildRequest(listOf(ChatMessage.user("你好")), null, stream = false)
        val buffer = okio.Buffer()
        request.body!!.writeTo(buffer)
        return buffer.readUtf8()
    }

    @Test
    fun `官方主力 id 也降档（PC 的前缀判断匹配不上它）`() {
        // PC 的条件是 startswith("deepseek-v4")，而官方现在叫 deepseek-flash
        assertEquals(ReasoningEffort.HIGH, ReasoningEffort.forModel("deepseek-flash"))
        assertEquals(ReasoningEffort.HIGH, ReasoningEffort.forModel("deepseek-v4-pro"))
        assertEquals(ReasoningEffort.HIGH, ReasoningEffort.forModel("deepseek-v4-flash-vision-exp"))
        // 大小写与空格不该影响判断
        assertEquals(ReasoningEffort.HIGH, ReasoningEffort.forModel("  DeepSeek-Flash "))
    }

    @Test
    fun `别的服务商不发这个字段`() {
        assertNull(ReasoningEffort.forModel("gpt-4o"))
        assertNull(ReasoningEffort.forModel("qwen-vl-max"))
        assertNull(ReasoningEffort.forModel(""))
    }

    @Test
    fun `请求体里确实带上了 high`() {
        val body = bodyOf("deepseek-flash")
        val root = PlannerJson.parseToJsonElement(body).jsonObject
        assertEquals(
            "high",
            (root["reasoning_effort"] as? JsonPrimitive)?.content,
        )
    }

    @Test
    fun `默认发 adaptive，而不是替用户强制开启`() {
        // 曾经这里断言"不发 thinking"（当时只在简单问题上量过，以为显式打开没收益）。
        // 后来发现官方 thinking.type 有 adaptive/enabled/disabled 三个变体，
        // 而且用户明确要在设置里能调——所以现在**有意**发出去，默认 adaptive。
        val body = bodyOf("deepseek-flash")
        assertTrue("应该带 thinking：$body", body.contains("\"thinking\""))
        assertTrue("默认是自适应：$body", body.contains("\"type\":\"adaptive\""))
        assertFalse("不该替用户强制开启：$body", body.contains("\"type\":\"enabled\""))
    }

    @Test
    fun `非 DeepSeek 模型的请求体里没有这个字段`() {
        val body = bodyOf("gpt-4o")
        assertFalse("不该给别的服务商发 DeepSeek 专有字段", body.contains("reasoning_effort"))
    }

    @Test
    fun `推理内容不会被当成正文（流式里 reasoning_content 必须被忽略）`() {
        // 实测 SSE 里真的有 reasoning_content，混进正文就等于把它的"心里话"打出来
        val chunk =
            """{"choices":[{"delta":{"reasoning_content":"让我想想…","content":"在。"},"index":0}]}"""
        val deltas = ChatStreamParser().feedLine("data: $chunk")

        assertEquals(1, deltas.size)
        assertTrue(deltas.single() is StreamDelta.Content)
        assertEquals("在。", (deltas.single() as StreamDelta.Content).text)
    }

    @Test
    fun `只有 reasoning_content 的块不产生任何正文`() {
        val chunk = """{"choices":[{"delta":{"reasoning_content":"先分析一下"},"index":0}]}"""
        assertTrue(ChatStreamParser().feedLine("data: $chunk").isEmpty())
    }

    @Test
    fun `三档的说明文案都在`() {
        assertTrue(ReasoningEffort.describe(ReasoningEffort.HIGH).contains("推荐"))
        assertTrue(ReasoningEffort.describe(ReasoningEffort.LOW).contains("最少"))
        assertTrue(ReasoningEffort.describe(ReasoningEffort.MAX).contains("最久"))
        assertEquals(3, ReasoningEffort.SUPPORTED.size)
    }
}
