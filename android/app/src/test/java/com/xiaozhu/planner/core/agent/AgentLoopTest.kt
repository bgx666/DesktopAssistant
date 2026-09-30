package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.LlmEvent
import com.xiaozhu.planner.core.llm.LlmStreamer
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.core.llm.ToolCallFunction
import com.xiaozhu.planner.core.llm.ToolSpec
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentLoopTest {

    /** 脚本化假模型：每次 stream() 依次吐出预先写好的一组事件。 */
    private class FakeLlm(private val rounds: List<List<LlmEvent>>) : LlmStreamer {
        var streamCalls = 0
            private set
        val sentMessages = mutableListOf<List<ChatMessage>>()

        override fun stream(messages: List<ChatMessage>, tools: List<ToolSpec>): Flow<LlmEvent> {
            sentMessages += messages
            val events = rounds.getOrElse(streamCalls) { listOf(LlmEvent.Finished("stop")) }
            streamCalls++
            return flow { events.forEach { emit(it) } }
        }
    }

    private class EchoTool : Tool {
        var calls = 0
        override val name = "web_search"
        override val description = "假搜索"
        override val parameters: JsonObject = buildJsonObject { put("type", "object") }
        override suspend fun run(args: JsonObject): String {
            calls++
            return "搜到 2 条：${args.str("query")}"
        }
    }

    private fun call(id: String = "call_1") = ToolCall(
        id = id,
        function = ToolCallFunction("web_search", """{"query":"小米 MiMo"}"""),
    )

    @Test
    fun `纯文本一轮就结束`() = runBlocking {
        val llm = FakeLlm(listOf(listOf(LlmEvent.TextDelta("你好"), LlmEvent.Finished("stop"))))
        val history = mutableListOf(ChatMessage.system("S"), ChatMessage.user("在吗"))
        val events = AgentLoop(llm, ToolRegistry(emptyList())).run(history).toList()

        assertEquals(1, llm.streamCalls)
        assertEquals(
            listOf(AgentEvent.TextDelta("你好"), AgentEvent.Done("你好")),
            events,
        )
        assertEquals(3, history.size)
        assertEquals("assistant", history.last().role)
    }

    @Test
    fun `调用工具后会带着结果再问一轮`() = runBlocking {
        val tool = EchoTool()
        val llm = FakeLlm(
            listOf(
                listOf(LlmEvent.TextDelta("我查一下"), LlmEvent.ToolCalls(listOf(call()))),
                listOf(LlmEvent.TextDelta("查到了"), LlmEvent.Finished("stop")),
            )
        )
        val history = mutableListOf(ChatMessage.user("小米有新语音模型吗"))
        val events = AgentLoop(llm, ToolRegistry(listOf(tool))).run(history).toList()

        assertEquals(1, tool.calls)
        assertEquals(2, llm.streamCalls)
        assertTrue(events.contains(AgentEvent.ToolStarted("web_search", """{"query":"小米 MiMo"}""")))
        assertTrue(events.any { it is AgentEvent.ToolFinished && (it.result).contains("搜到 2 条") })
        assertEquals(AgentEvent.Done("查到了"), events.last())

        // DeepSeek 硬约束：assistant(tool_calls) 后面必须紧跟同 id 的 tool 结果
        val assistantIdx = history.indexOfFirst { it.toolCalls != null }
        assertTrue("必须有带 tool_calls 的 assistant 消息", assistantIdx >= 0)
        val toolMsg = history[assistantIdx + 1]
        assertEquals("tool", toolMsg.role)
        assertEquals("call_1", toolMsg.toolCallId)
        assertTrue("工具结果要真的回喂给模型", llm.sentMessages[1].any { it.role == "tool" })
    }

    @Test
    fun `模型失败时直接报错且不污染历史`() = runBlocking {
        val llm = FakeLlm(listOf(listOf(LlmEvent.Failed("HTTP 401 未授权"))))
        val history = mutableListOf(ChatMessage.user("hi"))
        val events = AgentLoop(llm, ToolRegistry(emptyList())).run(history).toList()

        assertEquals(1, history.size)
        assertEquals(AgentEvent.Error("HTTP 401 未授权"), events.single())
    }

    @Test
    fun `反复调用工具会撞上最大轮数`() = runBlocking {
        val tool = EchoTool()
        val looping = listOf(LlmEvent.ToolCalls(listOf(call())))
        val llm = FakeLlm(listOf(looping, looping, looping, looping, looping))
        val history = mutableListOf(ChatMessage.user("循环"))
        val events = AgentLoop(llm, ToolRegistry(listOf(tool)), maxRounds = 2).run(history).toList()

        assertEquals(2, tool.calls)
        val last = events.last()
        assertTrue("应以错误收尾: $last", last is AgentEvent.Error)
        assertTrue((last as AgentEvent.Error).message.contains("2 轮"))
    }

    @Test
    fun `没有工具时也把 schema 列表留空传下去`() = runBlocking {
        val llm = FakeLlm(listOf(listOf(LlmEvent.TextDelta("嗯"), LlmEvent.Finished("stop"))))
        AgentLoop(llm, ToolRegistry(emptyList()))
            .run(mutableListOf(ChatMessage.user("hi"))).toList()
        assertEquals(1, llm.streamCalls)
    }
}