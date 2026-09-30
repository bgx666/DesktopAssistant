package com.xiaozhu.planner.core.memory

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.data.db.BufferStateEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 记忆树的 Room 层测试（Robolectric，真 SQLite，不是 mock）。
 * 覆盖：id 不重号、合并与停用、时间范围聚合、内容检索命中叶子原文、buffer 计数。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class MemoryStoreTest {

    private lateinit var db: PlannerDatabase
    private lateinit var store: MemoryStore

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        store = MemoryStore(db)
    }

    @After
    fun tearDown() = db.close()

    private fun out(summary: String, prefs: List<String> = emptyList()) = MemoryNodeOutput(
        summary = summary,
        profile = ProfileInfo(preferences = prefs),
    )

    @Test
    fun `叶子 id 递增，且重启后不重号`() = runBlocking {
        assertEquals("node0_001", store.addLeaf("第一条", 0 to 39))
        assertEquals("node0_002", store.addLeaf("第二条", 40 to 79))
        // 换一个新实例（等价于 App 重启）：序号必须从库里恢复
        val afterRestart = MemoryStore(db)
        assertEquals("node0_003", afterRestart.addLeaf("第三条", 80 to 119))
    }

    @Test
    fun `合并生成父节点并停用子节点`() = runBlocking {
        val a = store.addLeaf("A", 0 to 39, timeRange = "2026-09-13 20:00:00" to "2026-09-13 20:30:00")
        val b = store.addLeaf("B", 40 to 79, timeRange = "2026-09-14 09:00:00" to "2026-09-14 10:00:00")
        assertEquals(2, store.levelCount(0))

        val parent = store.compact(listOf(a, b), out("合并摘要", listOf("喜欢晚上学习")))

        assertEquals("node1_001", parent)
        assertEquals("合并后 level0 不该再有活跃节点", 0, store.levelCount(0))
        assertEquals(1, store.levelCount(1))
        // 子节点仍在库里（可回溯原文），只是不再活跃
        assertNotNull(store.nodeById(a))
        assertTrue(!store.nodeById(a)!!.isActive)
        assertEquals(parent, store.nodeById(a)!!.parentId)
        // 父节点时间范围 = 子节点 min/max
        assertEquals("2026-09-13 20:00:00" to "2026-09-14 10:00:00", store.effectiveTimeRange(parent))
        assertEquals(2, store.nodeInfo(parent)!!.children.size)
    }

    @Test
    fun `根节点是层级最高的那个`() = runBlocking {
        val a = store.addLeaf("A", 0 to 39)
        val b = store.addLeaf("B", 40 to 79)
        val parent = store.compact(listOf(a, b), out("父"))
        assertEquals(parent, store.rootId())
    }

    @Test
    fun `检索能命中只存在于叶子原文里的词`() = runBlocking {
        store.addLeaf(
            summary = "聊了求职进展",
            0 to 39,
            details = LeafDetails(
                messages = listOf(
                    ChatMessage.user("还在投呢，投了中电，宁德时代"),
                    ChatMessage.assistant("行，几个口子都开着不亏。"),
                )
            ),
        )
        store.addLeaf("聊了别的事", 40 to 79)

        // 关键词只在原文里，摘要里没有 —— 这正是 PC 版"只记得内容找不到"的场景
        val hits = store.search("宁德时代")
        assertEquals(1, hits.size)
        assertEquals("node0_001", hits[0].nodeId)
        assertTrue("片段里应带上下文", hits[0].snippet.contains("宁德时代"))

        // 多词 AND
        assertEquals(1, store.search("求职 宁德时代").size)
        assertEquals(0, store.search("求职 不存在的词").size)
    }

    @Test
    fun `时间范围缺失时从子节点聚合（对应导入的历史数据）`() = runBlocking {
        val a = store.addLeaf("A", 0 to 39, timeRange = "2026-08-01 09:00:00" to "2026-08-02 18:00:00")
        val b = store.addLeaf("B", 40 to 79, timeRange = "2026-08-03 08:30:00" to "2026-08-04 22:15:00")
        val parent = store.compact(listOf(a, b), out("父"))

        // 抹掉父节点自己的时间范围，模拟历史数据
        db.openHelper.writableDatabase.execSQL(
            "UPDATE nodes SET timeStart = NULL, timeEnd = NULL WHERE id = ?", arrayOf(parent)
        )
        assertNull(store.nodeById(parent)!!.timeStart)
        assertEquals(
            "2026-08-01 09:00:00" to "2026-08-04 22:15:00",
            store.effectiveTimeRange(parent),
        )
    }

    @Test
    fun `buffer 计数只算未压缩的原始消息`() = runBlocking {
        store.appendMessage("user", "在吗")
        store.appendMessage("assistant", "在")
        store.appendMessage("user", "帮我记一下", nodeId = null)
        store.appendMessage("assistant", "\n[摘要] 旧事", nodeId = "node0_001", nodeStart = 0)

        assertEquals(3, store.rawCount())
        assertEquals(4, store.rawMessages().size + store.nodeMessages().size)
        assertEquals(listOf("在吗", "在", "帮我记一下"), store.rawMessages().map { it.content })
        assertEquals(listOf("node0_001"), store.nodeMessages().map { it.nodeId })
    }

    @Test
    fun `buffer 状态可存可读`() = runBlocking {
        store.saveBufferState(
            BufferStateEntity(
                characterId = "assistant",
                msgCounter = 12,
                round = 3,
                compressedTotal = 40,
                heartbeatMinutes = 60.0,
                heartbeatNote = "看看进展",
            )
        )
        val loaded = store.loadBufferState()
        assertNotNull(loaded)
        assertEquals(40, loaded!!.compressedTotal)
        assertEquals("看看进展", loaded.heartbeatNote)
    }
}