package com.xiaozhu.planner.core.agent

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.core.llm.ToolCallFunction
import com.xiaozhu.planner.core.memory.LeafDetails
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** buffer 落盘/恢复：顺序、工具配对、被杀后的自愈。 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ConversationServiceTest {

    private lateinit var db: PlannerDatabase
    private lateinit var store: MemoryStore
    private lateinit var service: ConversationService

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java).allowMainThreadQueries().build()
        store = MemoryStore(db)
        service = ConversationService(store)
    }

    @After
    fun tearDown() = db.close()

    private fun call(id: String) = ToolCall(id = id, function = ToolCallFunction("web_search", "{}"))

    // ── 撤销（PC 版 /undo 同义）──────────────────────────────

    @Test
    fun `撤销删掉该条及其之后的原文`() = runBlocking {
        service.persist(
            listOf(
                ChatMessage.user("第一句"),
                ChatMessage.assistant("回第一句"),
                ChatMessage.user("第二句"),
                ChatMessage.assistant("回第二句"),
            ),
            System.currentTimeMillis(),
        )
        assertEquals(4, store.rawCount())

        assertTrue("撤销第 2 条（0 起）应删掉它和后面两条", service.truncateFrom(2))
        assertEquals(2, store.rawCount())
        assertEquals(
            listOf("第一句", "回第一句"),
            service.loadHistory().map { it.content },
        )
    }

    @Test
    fun `撤销第一条等于清空原文`() = runBlocking {
        service.persist(listOf(ChatMessage.user("a"), ChatMessage.assistant("b")), 0L)
        assertTrue(service.truncateFrom(0))
        assertEquals(0, store.rawCount())
        assertTrue(service.loadHistory().isEmpty())
    }

    @Test
    fun `撤销不动记忆节点（那是长期记忆，不是刚说的一句话）`() = runBlocking {
        // 模拟"压缩已经发生过"的状态：节点行 + buffer 里的 node 消息
        // （addLeaf 只写节点行；node 消息是压缩器 appendMessage 进去的）
        val nodeId = store.addLeaf(summary = "很久以前聊过的事", roundRange = 0 to 39)
        store.appendMessage(
            role = "assistant",
            content = "[$nodeId] [摘要] 很久以前聊过的事",
            nodeId = nodeId,
            nodeStart = 0,
        )
        service.persist(listOf(ChatMessage.user("刚说的"), ChatMessage.assistant("刚回的")), 0L)
        assertEquals(2, store.rawCount())

        assertTrue(service.truncateFrom(0))

        assertEquals("原文该清掉", 0, store.rawCount())
        assertEquals("记忆节点必须留着", 1, store.nodeCount())
        // 恢复出来的历史里，记忆节点那条还在（它会作为 node 消息进上下文）
        val history = service.loadHistory()
        assertEquals(1, history.size)
        assertTrue(history.single().content!!.contains("很久以前聊过的事"))
    }

    @Test
    fun `越界的下标什么都不做`() = runBlocking {
        service.persist(listOf(ChatMessage.user("只有一条")), 0L)
        assertTrue(!service.truncateFrom(5))
        assertTrue(!service.truncateFrom(-1))
        assertEquals(1, store.rawCount())
    }

    @Test
    fun `撤销后工具调用配对仍然合法`() = runBlocking {
        service.persist(
            listOf(
                ChatMessage.user("查一下"),
                ChatMessage.assistantToolCalls(listOf(call("c1"))),
                ChatMessage.tool("c1", "web_search", "结果"),
                ChatMessage.assistant("查到了"),
            ),
            0L,
        )
        // 从 tool 结果那条开始撤 → 悬空的 assistant(tool_calls) 会被 BufferRepair 清掉
        assertTrue(service.truncateFrom(2))
        val history = service.loadHistory()
        assertTrue(
            "剩下的历史里不能有声明了 tool_calls 却没有结果的 assistant",
            history.none { it.toolCalls?.isNotEmpty() == true },
        )
    }

    @Test
    fun `落盘后能原样恢复，且工具调用保持配对`() = runBlocking {
        service.persist(
            listOf(
                ChatMessage.user("帮我查一下"),
                ChatMessage.assistantToolCalls(listOf(call("c1")), "我查查"),
                ChatMessage.tool("c1", "web_search", "搜到 2 条"),
                ChatMessage.assistant("查到了，是 X。"),
            ),
            tsMillis = 1_700_000_000_000,
        )

        val restored = service.loadHistory()
        assertEquals(listOf("user", "assistant", "tool", "assistant"), restored.map { it.role })
        assertEquals("c1", restored[1].toolCalls!!.single().id)
        assertEquals("c1", restored[2].toolCallId)
        assertTrue(restored.last().content!!.contains("查到了"))
    }

    @Test
    fun `上次生成中途被杀留下的悬空 tool_calls 会被清掉`() = runBlocking {
        service.persist(listOf(ChatMessage.user("在吗")), tsMillis = 1)
        service.persist(listOf(ChatMessage.assistantToolCalls(listOf(call("c9")))), tsMillis = 2)

        val restored = service.loadHistory()
        assertEquals(1, restored.size)
        assertEquals("在吗", restored.single().content)
    }

    @Test
    fun `节点消息排在原始消息之前（时间顺序）`() = runBlocking {
        store.addLeaf(
            summary = "很久以前聊的",
            roundRange = 0 to 39,
            details = LeafDetails(messages = listOf(ChatMessage.user("旧话"))),
        )
        val node = store.nodeById("node0_001")!!
        store.appendMessage(
            role = "assistant",
            content = "[node0_001] 第0-39条\n[摘要] 很久以前聊的",
            tsMillis = 1,
            nodeId = node.id,
            nodeStart = 0,
        )
        service.persist(listOf(ChatMessage.user("现在说的话")), tsMillis = 2)

        val restored = service.loadHistory()
        assertEquals(2, restored.size)
        assertTrue("节点消息应在前", restored[0].content!!.contains("[摘要] 很久以前聊的"))
        assertEquals("现在说的话", restored[1].content)
    }

    @Test
    fun `system 消息不会落盘`() = runBlocking {
        service.persist(listOf(ChatMessage.system("角色卡"), ChatMessage.user("hi")), tsMillis = 1)
        assertEquals(1, service.loadHistory().size)
    }

    @Test
    fun `运行状态可存可读`() = runBlocking {
        val state = service.loadState().copy(round = 5, compressedTotal = 40, heartbeatNote = "看看进展")
        service.saveState(state)

        val loaded = service.loadState()
        assertEquals(5, loaded.round)
        assertEquals(40, loaded.compressedTotal)
        assertEquals("看看进展", loaded.heartbeatNote)
    }
}