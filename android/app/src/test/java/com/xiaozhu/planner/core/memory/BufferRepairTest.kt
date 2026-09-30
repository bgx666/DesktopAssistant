package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.core.llm.ToolCallFunction
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BufferRepairTest {

    private fun call(id: String) = ToolCall(id = id, function = ToolCallFunction("web_search", "{}"))

    @Test
    fun `正常对话原样保留`() {
        val msgs = listOf(ChatMessage.system("S"), ChatMessage.user("在吗"), ChatMessage.assistant("在"))
        assertEquals(msgs, BufferRepair.repair(msgs))
    }

    @Test
    fun `末尾悬空的 tool_calls 被丢掉（否则下次请求 400）`() {
        val msgs = listOf(
            ChatMessage.user("帮我查一下"),
            ChatMessage.assistantToolCalls(listOf(call("c1")), "我查查"),
        )
        val fixed = BufferRepair.repair(msgs)
        assertEquals(1, fixed.size)
        assertEquals("user", fixed.single().role)
    }

    @Test
    fun `配对的 assistant 与 tool 都保留`() {
        val msgs = listOf(
            ChatMessage.user("帮我查一下"),
            ChatMessage.assistantToolCalls(listOf(call("c1")), "我查查"),
            ChatMessage.tool("c1", "web_search", "搜到 2 条"),
            ChatMessage.assistant("查到了"),
        )
        assertEquals(msgs, BufferRepair.repair(msgs))
    }

    @Test
    fun `孤儿 tool 消息被丢掉`() {
        val msgs = listOf(
            ChatMessage.user("hi"),
            ChatMessage.tool("c9", "web_search", "没人要的结果"),
            ChatMessage.assistant("嗯"),
        )
        val fixed = BufferRepair.repair(msgs)
        assertEquals(listOf("user", "assistant"), fixed.map { it.role })
    }

    @Test
    fun `只回答了一半的多工具调用整组丢掉，且它的结果也不留`() {
        val msgs = listOf(
            ChatMessage.user("两个都查"),
            ChatMessage.assistantToolCalls(listOf(call("c1"), call("c2"))),
            ChatMessage.tool("c1", "web_search", "第一个结果"),
            ChatMessage.assistant("继续"),
        )
        val fixed = BufferRepair.repair(msgs)
        assertEquals(listOf("user", "assistant"), fixed.map { it.role })
        assertEquals("继续", fixed.last().content)
    }

    @Test
    fun `能识别末尾是否悬空`() {
        assertTrue(BufferRepair.hasTrailingToolCalls(listOf(ChatMessage.assistantToolCalls(listOf(call("c1"))))))
        assertFalse(BufferRepair.hasTrailingToolCalls(listOf(ChatMessage.assistant("说完了"))))
        assertFalse(BufferRepair.hasTrailingToolCalls(emptyList()))
    }
}