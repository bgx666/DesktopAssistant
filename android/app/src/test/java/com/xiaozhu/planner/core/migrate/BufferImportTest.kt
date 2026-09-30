package com.xiaozhu.planner.core.migrate

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.memory.MemoryStore
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
 * buffer 导入的两条关键规则（都踩过坑）：
 *
 * 1. **node 消息要按 node 消息导入**（带 nodeId/nodeStart）。当成普通原文的后果不是
 *    "显示难看"那么轻——下一次压缩会把那 24 条摘要**再总结一遍**，
 *    表现就是"把 8/8 到 9/27 的全部内容压成了一个节点"。
 * 2. **保留原始时间戳**（`data.metadata.ts`）。丢了它，之后新建的记忆节点时间范围
 *    全是"现在"，用户看到的时间跨度就完全不对。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class BufferImportTest {

    private lateinit var db: PlannerDatabase

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
    }

    @After
    fun tearDown() = db.close()

    private fun json(buffer: String) = """
        {"format":"planner-android-export","format_version":1,
         "nodes":[],"calendar_items":[],"warnings":[],"buffer":$buffer}
    """.trimIndent()

    @Test
    fun `带 metadata 的 node 消息按 node 消息导入`() = runBlocking {
        val buffer = """
            {"recent_buffer":[
              {"type":"human","data":{"content":"[00:17] 用户对你说：哈喽",
                 "metadata":{"ts":"2026-09-27 10:00:05"}}},
              {"type":"ai","data":{"content":"[node0_156] 第5137-5171条 [摘要] 9/27 上午…",
                 "metadata":{"node_id":"node0_156","node_start":5137,"ts":"2026-09-27 13:55:50"}}},
              {"type":"ai","data":{"content":"在。说吧","metadata":{"ts":"2026-09-27 13:56:00"}}}]}
        """.trimIndent()
        val payload = ExportReader.read(json(buffer).toByteArray())
        val mapped = PcImport.map(payload, MemoryStore.DEFAULT_CHARACTER)

        assertEquals(3, mapped.messages.size)
        val nodeMsg = mapped.messages[1]
        assertEquals("node0_156", nodeMsg.nodeId)
        assertEquals(5137, nodeMsg.nodeStart)
        assertNull("普通消息不该带 nodeId", mapped.messages[0].nodeId)
        assertNull(mapped.messages[2].nodeId)
    }

    @Test
    fun `node 消息不会参与压缩（压缩只取 nodeId 为空的原文）`() = runBlocking {
        val buffer = """
            {"recent_buffer":[
              {"type":"human","data":{"content":"真的对话","metadata":{"ts":"2026-09-27 10:00:05"}}},
              {"type":"ai","data":{"content":"[node0_156] 第5137-5171条 [摘要] 很久以前",
                 "metadata":{"node_id":"node0_156","node_start":5137,"ts":"2026-09-27 13:55:50"}}}]}
        """.trimIndent()
        Importer(db).apply(ExportReader.read(json(buffer).toByteArray()), wipeFirst = false)

        val store = MemoryStore(db)
        // rawMessages 是"要压缩的那批"：必须只剩真正的原文
        assertEquals(1, store.rawMessages().size)
        assertEquals("真的对话", store.rawMessages().single().content)
        assertEquals("node 消息仍在 buffer 里（界面上的记忆卡片）", 1, store.nodeMessages().size)
        assertEquals("node0_156", store.nodeMessages().single().nodeId)
    }

    @Test
    fun `原始时间戳被保留，而不是统一写成当前时间`() = runBlocking {
        val buffer = """
            {"recent_buffer":[
              {"type":"human","data":{"content":"八月的一句","metadata":{"ts":"2026-08-08 10:23:35"}}}]}
        """.trimIndent()
        val mapped = PcImport.map(
            ExportReader.read(json(buffer).toByteArray()),
            MemoryStore.DEFAULT_CHARACTER,
        )
        val expected = java.time.LocalDateTime.parse("2026-08-08T10:23:35")
            .atZone(java.time.ZoneId.of("Asia/Shanghai")).toInstant().toEpochMilli()
        assertEquals(expected, mapped.messages.single().tsMillis)
    }

    @Test
    fun `没有 metadata 时退回当前时间，不崩`() = runBlocking {
        val buffer = """{"recent_buffer":[{"type":"human","data":{"content":"没有时间"}}]}"""
        val mapped = PcImport.map(
            ExportReader.read(json(buffer).toByteArray()),
            MemoryStore.DEFAULT_CHARACTER,
        )
        assertTrue(mapped.messages.single().tsMillis > 0)
        assertNull(mapped.messages.single().nodeId)
    }

    @Test
    fun `时间戳格式不对时退回当前时间`() = runBlocking {
        val buffer = """
            {"recent_buffer":[{"type":"human","data":{"content":"x","metadata":{"ts":"昨天"}}}]}
        """.trimIndent()
        val mapped = PcImport.map(
            ExportReader.read(json(buffer).toByteArray()),
            MemoryStore.DEFAULT_CHARACTER,
        )
        assertNotNull(mapped.messages.single())
        assertTrue(mapped.messages.single().tsMillis > 0)
    }
}
