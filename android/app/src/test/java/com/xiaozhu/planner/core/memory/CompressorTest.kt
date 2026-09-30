package com.xiaozhu.planner.core.memory

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.LlmCompleter
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

/**
 * 压缩流程测试（Robolectric + 真 SQLite + 脚本化假模型）。
 * 重点：阈值行为、buffer 是否被正确腾挪、**失败时绝不破坏上下文**。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CompressorTest {

    private lateinit var db: PlannerDatabase
    private lateinit var store: MemoryStore

    private val leafJson =
        """{"summary":"压缩后的摘要","profile":{"preferences":["喜欢晚上学习"]},"future_notes":["后来改成 16:00"]}"""
    private val parentJson =
        """{"summary":"上卷摘要","profile":{"goals":["三个月内学完 Python"]}}"""

    private class FakeCompleter(
        private val leafJson: String,
        private val parentJson: String,
        private val garbage: Boolean = false,
        private val boom: Boolean = false,
    ) : LlmCompleter {
        var calls = 0
            private set
        val prompts = mutableListOf<String>()

        override suspend fun complete(messages: List<ChatMessage>): String {
            calls++
            val text = messages.joinToString("\n") { it.content.orEmpty() }
            prompts += text
            if (boom) throw IllegalStateException("网络炸了")
            if (garbage) return "模型今天不想输出 JSON"
            return if (text.contains("个节点压缩")) parentJson else leafJson
        }
    }

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

    private suspend fun seedMessages(n: Int, startTs: Long = 1_700_000_000_000) {
        repeat(n) { i ->
            val role = if (i % 2 == 0) "user" else "assistant"
            store.appendMessage(role, "第 $i 条消息", tsMillis = startTs + i * 60_000L)
        }
    }

    private fun compressor(fake: LlmCompleter, settings: CompressionSettings = CompressionSettings()) =
        Compressor(store, fake, systemPrompt = "（角色卡）", settings = settings)

    @Test
    fun `消息不够阈值时什么都不做`() = runBlocking {
        seedMessages(10)
        val fake = FakeCompleter(leafJson, parentJson)
        val outcomes = compressor(fake).compressIfNeeded()

        assertEquals(listOf(CompressionOutcome.Skipped), outcomes)
        assertEquals(0, fake.calls)
        assertEquals(0, store.nodeCount())
    }

    @Test
    fun `达到阈值时压成叶子并腾出 buffer`() = runBlocking {
        seedMessages(60)
        val fake = FakeCompleter(leafJson, parentJson)
        val outcomes = compressor(fake).compressIfNeeded()

        val leaf = outcomes.filterIsInstance<CompressionOutcome.LeafCreated>().single()
        assertEquals("node0_001", leaf.nodeId)
        assertEquals(40, leaf.messageCount)

        // buffer：60 → 留 20 条原文 + 1 条节点消息
        assertEquals(20, store.rawCount())
        assertEquals(1, store.nodeMessages().size)
        assertEquals("node0_001", store.nodeMessages().single().nodeId)

        // 节点：序号区间、原文、画像、后续说明都对
        val node = store.nodeById("node0_001")!!
        assertEquals(0, node.roundStart)
        assertEquals(39, node.roundEnd)
        assertEquals("压缩后的摘要", node.summary)
        val details = MemoryJson.decodeDetails(node.details)
        assertEquals(40, details.messages.size)
        assertEquals(listOf("后来改成 16:00"), details.futureNotes)
        assertEquals(listOf("喜欢晚上学习"), MemoryJson.decodeProfile(node.profile)!!.preferences)

        // 节点消息进上下文时带画像与后续说明（模型"记得人"的唯一来源）
        val nodeMsg = store.nodeMessages().single().content
        assertTrue(nodeMsg.contains("[摘要] 压缩后的摘要"))
        assertTrue(nodeMsg.contains("[喜好] 喜欢晚上学习"))
        assertTrue(nodeMsg.contains("[后续说明] 后来改成 16:00"))

        assertEquals(40, store.loadBufferState()!!.compressedTotal)
    }

    @Test
    fun `同层满阈值时上卷成父节点`() = runBlocking {
        // 直接铺 8 个叶子（等价于压过 8 轮）
        repeat(8) { i ->
            store.addLeaf("叶子$i", (i * 40) to (i * 40 + 39))
        }
        val fake = FakeCompleter(leafJson, parentJson)
        val outcomes = compressor(fake).compressIfNeeded()

        val parent = outcomes.filterIsInstance<CompressionOutcome.ParentCreated>().single()
        assertEquals("node1_001", parent.nodeId)
        assertEquals(1, parent.level)
        assertEquals(4, parent.childCount)
        assertEquals(4, store.levelCount(0))
        assertEquals(1, store.levelCount(1))
        assertEquals("上卷摘要", store.nodeById("node1_001")!!.summary)
    }

    @Test
    fun `压缩调用抛异常时不动 buffer`() = runBlocking {
        seedMessages(60)
        val before = store.rawCount()
        val outcomes = compressor(FakeCompleter(leafJson, parentJson, boom = true)).compressIfNeeded()

        assertTrue(outcomes.any { it is CompressionOutcome.Failed })
        assertEquals("原文不能被删", before, store.rawCount())
        assertEquals(0, store.nodeCount())
        assertEquals(0, store.loadBufferState()?.compressedTotal ?: 0)
    }

    @Test
    fun `模型返回非 JSON 时退化成整段摘要（宁可粗糙也别丢记忆）`() = runBlocking {
        seedMessages(60)
        val outcomes = compressor(FakeCompleter(leafJson, parentJson, garbage = true)).compressIfNeeded()

        assertTrue(outcomes.any { it is CompressionOutcome.LeafCreated })
        assertEquals("模型今天不想输出 JSON", store.nodeById("node0_001")!!.summary)
        assertEquals(20, store.rawCount())
    }

    @Test
    fun `压缩提示词里带上了未来消息与字段说明`() = runBlocking {
        seedMessages(60)
        val fake = FakeCompleter(leafJson, parentJson)
        compressor(fake).compressIfNeeded()

        val prompt = fake.prompts.first()
        assertTrue("应说明后续消息的用途", prompt.contains("之后"))
        assertTrue("应带字段说明", prompt.contains("future_notes"))
        assertTrue("应带待压缩原文", prompt.contains("第 0 条消息"))
        assertTrue("应包含保留下来的未来消息", prompt.contains("第 59 条消息"))
    }
}