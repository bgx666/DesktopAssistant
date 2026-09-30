package com.xiaozhu.planner.core.tools

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.agent.int
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.memory.LeafDetails
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.ProfileInfo
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class MemoryToolsTest {

    private lateinit var db: PlannerDatabase
    private lateinit var store: MemoryStore

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java).allowMainThreadQueries().build()
        store = MemoryStore(db)
    }

    @After
    fun tearDown() = db.close()

    private fun args(json: String): JsonObject = PlannerJson.parseToJsonElement(json) as JsonObject

    private suspend fun seedTree() {
        val a = store.addLeaf(
            summary = "聊了求职进展",
            roundRange = 0 to 39,
            details = LeafDetails(messages = listOf(ChatMessage.user("还在投呢，投了中电，宁德时代"))),
            profile = ProfileInfo(goals = listOf("三个月内学完 Python")),
            timeRange = "2026-09-13 20:00:00" to "2026-09-13 20:30:00",
        )
        val b = store.addLeaf("聊了别的事", 40 to 79)
        store.compact(listOf(a, b), com.xiaozhu.planner.core.memory.MemoryNodeOutput(summary = "两个月的线"))
    }

    @Test
    fun `没有节点时明确说明还没触发压缩`() = runBlocking {
        val out = ExploreMemoryTreeTool(store).run(args("{}"))
        assertTrue(out.contains("尚未触发压缩"))
    }

    @Test
    fun `留空 node_id 时从根开始给概览`() = runBlocking {
        seedTree()
        val out = ExploreMemoryTreeTool(store).run(args("{}"))
        assertTrue("应展开到根节点", out.contains("node1_001"))
        assertTrue("应列出子节点", out.contains("子节点"))
        assertTrue("应能看到子节点编号", out.contains("node0_001"))
    }

    @Test
    fun `展开叶子能看到当时的原文与画像`() = runBlocking {
        seedTree()
        val out = ExploreMemoryTreeTool(store).run(args("""{"node_id":"node0_001"}"""))
        assertTrue(out.contains("原文（1 条）"))
        assertTrue(out.contains("宁德时代"))
        assertTrue(out.contains("目标：三个月内学完 Python"))
    }

    @Test
    fun `找不到节点时给出提示而不是抛错`() = runBlocking {
        seedTree()
        val out = ExploreMemoryTreeTool(store).run(args("""{"node_id":"node9_999"}"""))
        assertTrue(out.contains("找不到节点"))
    }

    @Test
    fun `检索工具命中只存在于原文里的词并给出节点号`() = runBlocking {
        seedTree()
        val out = SearchMemoryTool(store).run(args("""{"query":"宁德时代"}"""))
        assertTrue(out.contains("命中"))
        assertTrue(out.contains("node0_001"))
        assertTrue(out.contains("explore_memory_tree"))
    }

    @Test
    fun `检索不到时给可操作的建议`() = runBlocking {
        seedTree()
        val out = SearchMemoryTool(store).run(args("""{"query":"完全不存在的东西"}"""))
        assertTrue(out.contains("没有找到"))
        assertTrue(out.contains("换个说法"))
    }

    @Test
    fun `检索缺参数时提示补参数`() = runBlocking {
        val out = SearchMemoryTool(store).run(args("{}"))
        assertTrue(out.contains("query"))
    }
}