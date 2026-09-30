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