package com.xiaozhu.planner.core.llm

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * SSE 流解析的回归测试——重点覆盖 tool_calls 的**碎片拼装**（最容易写错的地方）。
 */
class ChatStreamParserTest {

    private fun content(payload: String) = """data: {"choices":[{"delta":{"content":"$payload"}}]}"""

    @Test
    fun `纯文本增量按顺序吐出`() {
        val p = ChatStreamParser()
        val out = mutableListOf<StreamDelta>()
        out += p.feedLine(content("你"))
        out += p.feedLine(content("好"))
        out += p.feedLine("data: [DONE]")

        val texts = out.filterIsInstance<StreamDelta.Content>().map { it.text }
        assertEquals(listOf("你", "好"), texts)
        assertTrue(out.last() is StreamDelta.Done)
    }

    @Test
    fun `空行与注释被忽略`() {
        val p = ChatStreamParser()
        assertTrue(p.feedLine("").isEmpty())
        assertTrue(p.feedLine("   ").isEmpty())
        assertTrue(p.feedLine(": keep-alive").isEmpty())
        assertTrue(p.feedLine("event: message").isEmpty())
    }

    @Test
    fun `半截 JSON 不会打断整轮`() {
        val p = ChatStreamParser()
        assertTrue(p.feedLine("""data: {"choices":[{"delta":{"cont""").isEmpty())
        val out = p.feedLine(content("恢复"))
        assertEquals(listOf("恢复"), out.filterIsInstance<StreamDelta.Content>().map { it.text })
    }

    @Test
    fun `tool_calls 碎片按 index 拼装成完整参数`() {
        val p = ChatStreamParser()
        // 第一片：id + 函数名 + 参数开头
        p.feedLine(
            """data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",""" +
                """"function":{"name":"add_schedule","arguments":"{\"date\":\"2026-"}}]}}]}"""
        )
        // 第二片：只有参数中段
        p.feedLine(
            """data: {"choices":[{"delta":{"tool_calls":[{"index":0,""" +
                """"function":{"arguments":"10-01\",\"content\":"}}]}}]}"""
        )
        // 第三片：参数收尾
        val out = p.feedLine(
            """data: {"choices":[{"delta":{"tool_calls":[{"index":0,""" +
                """"function":{"arguments":"\"开会\"}"}}]}}]}"""
        )

        // finish_reason=tool_calls 时就该把工具交出来
        val finished = p.feedLine("""data: {"choices":[{"finish_reason":"tool_calls"}]}""")

        assertTrue("第三片不该提前吐工具: $out", out.none { it is StreamDelta.ToolCalls })
        val calls = finished.filterIsInstance<StreamDelta.ToolCalls>().single().calls
        assertEquals(1, calls.size)
        assertEquals("call_1", calls[0].id)
        assertEquals("add_schedule", calls[0].function.name)
        assertEquals("""{"date":"2026-10-01","content":"开会"}""", calls[0].function.arguments)
    }

    @Test
    fun `多个并行工具按 index 排序`() {
        val p = ChatStreamParser()
        p.feedLine(
            """data: {"choices":[{"delta":{"tool_calls":[""" +
                """{"index":1,"id":"b","function":{"name":"web_search","arguments":"{}"}},""" +
                """{"index":0,"id":"a","function":{"name":"heartbeat","arguments":"{}"}}""" +
                """]}}]}"""
        )
        val out = p.feedLine("data: [DONE]")
        val calls = out.filterIsInstance<StreamDelta.ToolCalls>().single().calls
        assertEquals(listOf("a", "b"), calls.map { it.id })
        assertEquals(listOf("heartbeat", "web_search"), calls.map { it.function.name })
    }

    @Test
    fun `没有 DONE 时 finish 仍能收尾`() {
        val p = ChatStreamParser()
        p.feedLine(
            """data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"x",""" +
                """"function":{"name":"fetch_web","arguments":"{\"url\":\"https://a.b\"}"}}]}}]}"""
        )
        val out = p.finish()
        val calls = out.filterIsInstance<StreamDelta.ToolCalls>().single().calls
        assertEquals("fetch_web", calls[0].function.name)
        assertEquals("""{"url":"https://a.b"}""", calls[0].function.arguments)
        assertTrue(out.last() is StreamDelta.Done)
        // 重复 finish 不应重复吐
        assertTrue(p.finish().isEmpty())
    }

    @Test
    fun `参数缺失时给空对象而不是空串`() {
        val p = ChatStreamParser()
        p.feedLine(
            """data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"z",""" +
                """"function":{"name":"heartbeat"}}]}}]}"""
        )
        val calls = p.finish().filterIsInstance<StreamDelta.ToolCalls>().single().calls
        assertEquals("{}", calls[0].function.arguments)
    }
}