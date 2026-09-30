package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.core.llm.ToolCallFunction
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ToolRegistryTest {

    private class FakeTool(
        override val name: String,
        private val result: String = "ok",
        private val boom: Boolean = false,
    ) : Tool {
        var lastArgs: JsonObject? = null
        override val description = "假工具"
        override val parameters: JsonObject = buildJsonObject { put("type", "object") }
        override suspend fun run(args: JsonObject): String {
            lastArgs = args
            if (boom) throw IllegalStateException("炸了")
            return result
        }
    }

    private fun call(name: String, args: String) =
        ToolCall(id = "c1", function = ToolCallFunction(name = name, arguments = args))

    @Test
    fun `schema 按注册顺序生成`() {
        val reg = ToolRegistry(listOf(FakeTool("a"), FakeTool("b")))
        assertEquals(listOf("a", "b"), reg.specs().map { it.function.name })
        assertEquals(listOf("a", "b"), reg.names())
    }

    @Test
    fun `调用不存在的工具时给出可用清单`() = runBlocking {
        val reg = ToolRegistry(listOf(FakeTool("web_search")))
        val out = reg.execute(call("add_schedule", "{}"))
        assertTrue(out.contains("没有名为 add_schedule 的工具"))
        assertTrue(out.contains("web_search"))
    }

    @Test
    fun `参数不是合法 JSON 时返回可读错误而不再往下传`() = runBlocking {
        val tool = FakeTool("t")
        val reg = ToolRegistry(listOf(tool))
        val out = reg.execute(call("t", "{不是 json"))
        assertTrue(out.contains("不是合法 JSON"))
        assertEquals(null, tool.lastArgs)
    }

    @Test
    fun `工具抛异常时转成文本结果`() = runBlocking {
        val reg = ToolRegistry(listOf(FakeTool("t", boom = true)))
        val out = reg.execute(call("t", "{}"))
        assertTrue(out.contains("执行失败"))
        assertTrue(out.contains("IllegalStateException"))
    }

    @Test
    fun `正常执行会收到解析后的参数`() = runBlocking {
        val tool = FakeTool("t", result = "结果是 42")
        val reg = ToolRegistry(listOf(tool))
        val out = reg.execute(call("t", """{"query":"小米","limit":3}"""))
        assertEquals("结果是 42", out)
        assertEquals("小米", tool.lastArgs?.str("query"))
        assertEquals(3, tool.lastArgs?.int("limit", 0))
    }
}