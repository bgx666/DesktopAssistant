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
 * 实测（DeepSeek 官方，同一个问题各 3 次）：默认档是 `high`（不发字段也有 170–270
 * reasoning token）；`reasoning_effort=low` 更快更省；而 `thinking:{type:enabled}`
 * 没有收益、实测那次推理反而更多——所以**只发 effort，不发 thinking**。
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
        assertEquals(ReasoningEffort.LOW, ReasoningEffort.forModel("deepseek-flash"))
        assertEquals(ReasoningEffort.LOW, ReasoningEffort.forModel("deepseek-v4-pro"))
        assertEquals(ReasoningEffort.LOW, ReasoningEffort.forModel("deepseek-v4-flash-vision-exp"))
        // 大小写与空格不该影响判断
        assertEquals(ReasoningEffort.LOW, ReasoningEffort.forModel("  DeepSeek-Flash "))
    }

    @Test
    fun `别的服务商不发这个字段`() {
        assertNull(ReasoningEffort.forModel("gpt-4o"))
        assertNull(ReasoningEffort.forModel("qwen-vl-max"))
        assertNull(ReasoningEffort.forModel(""))
    }

    @Test
    fun `请求体里确实带上了 low`() {
        val body = bodyOf("deepseek-flash")
        val root = PlannerJson.parseToJsonElement(body).jsonObject
        assertEquals(
            "low",
            (root["reasoning_effort"] as? JsonPrimitive)?.content,
        )
    }

    @Test
    fun `不发 thinking 字段（实测没收益，反而推理更多）`() {
        val body = bodyOf("deepseek-flash")
        assertFalse("不该带 thinking：$body", body.contains("\"thinking\""))
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
        assertTrue(ReasoningEffort.describe(ReasoningEffort.LOW).contains("省"))
        assertTrue(ReasoningEffort.describe(ReasoningEffort.HIGH).contains("默认"))
        assertTrue(ReasoningEffort.describe(ReasoningEffort.MAX).contains("最久"))
        assertEquals(3, ReasoningEffort.SUPPORTED.size)
    }
}
