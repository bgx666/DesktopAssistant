package com.xiaozhu.planner.core.migrate

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.memory.MemoryJson
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.MessageMapping
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.ByteArrayOutputStream
import java.util.zip.GZIPOutputStream

/**
 * 从电脑搬旧数据：**映射**与**落库**两段都要对。
 *
 * 迁移只做一次，错了很难复盘（用户看到的是"它不记得我们聊过的事"），
 * 所以这里用一份贴近真实导出的 JSON 走完整条路：读 → 映射 → 写库 → 查回来。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class PcImportTest {

    private lateinit var db: PlannerDatabase
    private lateinit var importer: Importer

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        importer = Importer(db)
    }

    @After
    fun tearDown() = db.close()

    private fun json(
        nodes: String = "[]",
        buffer: String = "null",
        calendar: String = "[]",
        version: Int = 1,
        format: String = "planner-android-export",
    ) = """
        {"format":"$format","format_version":$version,"exported_at":"2026-09-30T10:00:00+08:00",
         "source":{"memory_tree":"D:\\planner-release\\data\\memory_tree.db"},
         "counts":{"nodes":1},
         "nodes":$nodes,"buffer":$buffer,"calendar_items":$calendar,"warnings":[]}
    """.trimIndent()

    private val oneNode = """
        [{"character_id":"assistant","id":"node0_001","level":0,"summary":"聊了投递进度",
          "parent_id":null,"round_start":0,"round_end":39,
          "time_start":"2026-09-28 09:00:00","time_end":"2026-09-28 09:40:00",
          "details":{"messages":[{"role":"user","content":"投了中电"},
                                  {"role":"assistant","content":"行，记下了"}],
                     "future_notes":["后来改到周一投"]},
          "profile":{"preferences":["喜欢直接说"],"personality":[],"habits":["晚上复习"],"goals":["找工作"]},
          "meta":{"schema_version":2},"is_active":1,"created_at":1759020000}]
    """.trimIndent()

    @Test
    fun `读得懂导出文件并映射成安卓的表`() = runBlocking {
        val payload = ExportReader.read(json(nodes = oneNode).toByteArray())
        val mapped = PcImport.map(payload, MemoryStore.DEFAULT_CHARACTER)

        val node = mapped.nodes.single()
        assertEquals("node0_001", node.id)
        assertEquals(0, node.level)
        assertEquals("聊了投递进度", node.summary)
        assertEquals(0, node.roundStart)
        assertEquals(39, node.roundEnd)
        assertTrue(node.isActive)

        // 原文与后续说明都要在（这是"它还记得细节"的来源）
        val details = MemoryJson.decodeDetails(node.details)
        assertEquals(2, details.messages.size)
        assertEquals("user", details.messages.first().role)
        assertEquals("后来改到周一投", details.futureNotes.single())

        // 画像四维度
        val profile = MemoryJson.decodeProfile(node.profile)
        assertNotNull(profile)
        assertEquals(listOf("喜欢直接说"), profile!!.preferences)
        assertEquals(listOf("晚上复习"), profile.habits)

        // 检索文本要重建（PC 库里没有这一列）
        assertTrue(node.searchText.contains("中电"))
        assertTrue(node.searchText.contains("喜欢直接说"))
    }

    @Test
    fun `写进库后能被记忆树读出来`() = runBlocking {
        val payload = ExportReader.read(json(nodes = oneNode).toByteArray())
        val result = importer.apply(payload, wipeFirst = false)

        assertEquals(1, result.nodes)
        val store = MemoryStore(db)
        assertEquals(1, store.nodeCount())
        val info = store.nodeInfo("node0_001")!!
        assertEquals("聊了投递进度", info.summary)
        assertEquals(2, info.details.size)
        assertEquals("后来改到周一投", info.futureNotes.single())
        // 导入的记忆要能被检索到（不然"记得"就是空话）
        assertTrue(store.search("中电").isNotEmpty())
    }

    @Test
    fun `buffer 里的 LangChain 消息转成 OpenAI 角色`() = runBlocking {
        val buffer = """
            {"character_id":"assistant",
             "recent_buffer":[
               {"type":"human","data":{"content":"在吗","type":"human"}},
               {"type":"ai","data":{"content":"在","tool_calls":[
                   {"name":"list_schedule","args":{"start_date":"2026-09-30"},"id":"call_1"}],
                   "type":"ai"}},
               {"type":"tool","data":{"content":"共 2 项","tool_call_id":"call_1","name":"list_schedule","type":"tool"}}],
             "msg_counter":42,"round":7,"compressed_total":40,
             "last_activity_at":1759200000.5,"next_heartbeat_at":1759203600.0,
             "heartbeat_minutes":60.0,"heartbeat_note":"看看进度",
             "last_player_message_at":1759199000.0}
        """.trimIndent()
        val payload = ExportReader.read(json(buffer = buffer).toByteArray())
        val mapped = PcImport.map(payload, MemoryStore.DEFAULT_CHARACTER)

        assertEquals(3, mapped.messages.size)
        assertEquals(listOf("user", "assistant", "tool"), mapped.messages.map { it.role })

        // 工具调用必须是 OpenAI 形状：arguments 是**字符串**（给成对象下一轮请求直接 400）
        val assistant = MessageMapping.toChatMessage(mapped.messages[1])
        assertNotNull(assistant.toolCalls)
        val call = assistant.toolCalls!!.single()
        assertEquals("call_1", call.id)
        assertEquals("list_schedule", call.function.name)
        assertTrue(call.function.arguments.contains("2026-09-30"))
        assertEquals("call_1", mapped.messages[2].toolCallId)
        assertEquals("list_schedule", mapped.messages[2].name)

        // epoch 秒 → 毫秒
        val state = mapped.bufferState!!
        assertEquals(1759200000500L, state.lastActivityAt)
        assertEquals(1759203600000L, state.nextHeartbeatAt)
        assertEquals(1759199000000L, state.lastPlayerMessageAt)
        assertEquals("看看进度", state.heartbeatNote)
        assertEquals(7, state.round)
    }

    @Test
    fun `导入后恢复出的历史保持工具调用配对`() = runBlocking {
        val buffer = """
            {"recent_buffer":[
               {"type":"ai","data":{"content":"","tool_calls":[{"name":"heartbeat","args":{"minutes":30},"id":"c1"}]}},
               {"type":"tool","data":{"content":"好，30 分钟后我再来。","tool_call_id":"c1","name":"heartbeat"}}]}
        """.trimIndent()
        importer.apply(ExportReader.read(json(buffer = buffer).toByteArray()), wipeFirst = false)

        val history = com.xiaozhu.planner.core.agent.ConversationService(MemoryStore(db)).loadHistory()
        assertEquals(2, history.size)
        assertEquals("assistant", history[0].role)
        assertEquals("tool", history[1].role)
        assertEquals("c1", history[0].toolCalls!!.single().id)
        assertEquals("c1", history[1].toolCallId)
    }

    @Test
    fun `日历项连重复系列一起搬过来`() = runBlocking {
        val calendar = """
            [{"id":3,"date":"2026-09-30","time":"20:00","kind":"event","content":"复习线代",
              "detail":"第三章","status":"todo","reminded_at":null,
              "created_at":"2026-09-28 09:00:00","done_at":null,"series_id":null,"repeat":null,"repeat_until":null},
             {"id":4,"date":"2026-09-30","time":"08:00","kind":"task","content":"吃药",
              "detail":"","status":"done","reminded_at":"2026-09-30 08:00",
              "created_at":"2026-09-28 09:00:00","done_at":"2026-09-30 08:05:00",
              "series_id":"abc123","repeat":"daily","repeat_until":"2027-09-30"}]
        """.trimIndent()
        importer.apply(ExportReader.read(json(calendar = calendar).toByteArray()), wipeFirst = false)

        val items = db.calendar().rangeAll("2026-01-01", "2027-01-01")
        assertEquals(2, items.size)
        val series = items.single { it.seriesId != null }
        assertEquals("abc123", series.seriesId)
        assertEquals("daily", series.repeat)
        assertEquals("2027-09-30", series.repeatUntil)
        assertEquals(CalendarItemEntity.STATUS_DONE, series.status)
        assertNotNull(series.doneAt)          // 文本日期 → epoch 毫秒
        // 2026-09-30 08:05:00 +08:00
        assertEquals(1790726700000L, series.doneAt!!)
    }

    @Test
    fun `本地已有数据时预览会报出来，追加导入不清空`() = runBlocking {
        importer.apply(ExportReader.read(json(nodes = oneNode).toByteArray()), wipeFirst = false)
        val payload = ExportReader.read(json(nodes = oneNode).toByteArray())

        val preview = importer.preview(payload)
        assertEquals(1, preview.nodes)
        assertEquals(1, preview.localNodes)
        assertTrue(preview.hasLocalData)

        // 追加：不清空（节点 id 相同会被覆盖，这是可接受的）
        importer.apply(payload, wipeFirst = false)
        assertEquals(1, MemoryStore(db).nodeCount())
    }

    @Test
    fun `清空后导入会先删掉本地已有的一切`() = runBlocking {
        // 先造一点"手机上的新数据"
        val store = MemoryStore(db)
        store.appendMessage("user", "这是手机上聊的")
        db.calendar().insert(
            CalendarItemEntity(date = "2026-10-05", content = "手机上加的")
        )
        assertEquals(1, store.rawCount())

        importer.apply(ExportReader.read(json(nodes = oneNode).toByteArray()), wipeFirst = true)

        assertEquals(1, store.nodeCount())
        assertEquals("导入后不该残留手机上的旧对话", 0, store.rawCount())
        assertTrue(db.calendar().rangeAll("2026-01-01", "2027-01-01").isEmpty())
    }

    @Test
    fun `gzip 导出文件按魔数识别，不看扩展名`() = runBlocking {
        val plain = json(nodes = oneNode).toByteArray()
        val gz = ByteArrayOutputStream().also { out ->
            GZIPOutputStream(out).use { it.write(plain) }
        }.toByteArray()

        assertTrue(ExportReader.isGzip(gz))
        assertFalse(ExportReader.isGzip(plain))
        assertEquals(1, ExportReader.read(gz).nodes.size)
        assertEquals(1, ExportReader.read(plain).nodes.size)
    }

    @Test
    fun `不是导出文件时给清楚的错误而不是崩`() {
        val cases = listOf(
            "" to "空",
            "not json" to "JSON",
            """{"hello":1}""" to "format",
            json(version = 99) to "版本",
        )
        cases.forEach { (content, expect) ->
            val e = runCatching { ExportReader.read(content.toByteArray()) }.exceptionOrNull()
            assertTrue("$content 应该被拒绝", e is ExportReader.BadFile)
            assertTrue("错误信息要说明原因：${e?.message}", e!!.message!!.contains(expect))
        }
    }

    @Test
    fun `认不出的消息形状被跳过并记下来，而不是当成正文塞进去`() = runBlocking {
        val buffer = """
            {"recent_buffer":[
               {"type":"human","data":{"content":"正常的一条"}},
               {"foo":"bar"},
               {"type":"alien","data":{"content":"外星消息"}}]}
        """.trimIndent()
        val payload = ExportReader.read(json(buffer = buffer).toByteArray())
        val mapped = PcImport.map(payload, MemoryStore.DEFAULT_CHARACTER)

        assertEquals(1, mapped.messages.size)
        assertEquals("正常的一条", mapped.messages.single().content)
        assertEquals(2, mapped.skipped.size)
    }

    @Test
    fun `content 是文本块数组时也能拼出来`() = runBlocking {
        val buffer = """
            {"recent_buffer":[{"type":"ai","data":{"content":[
                {"type":"text","text":"第一段"},{"type":"text","text":"第二段"}]}}]}
        """.trimIndent()
        val mapped = PcImport.map(
            ExportReader.read(json(buffer = buffer).toByteArray()),
            MemoryStore.DEFAULT_CHARACTER,
        )
        assertEquals("第一段\n第二段", mapped.messages.single().content)
    }

    @Test
    fun `没有原文的导出（no-details）不会让叶子变成空壳而不自知`() = runBlocking {
        val nodes = """
            [{"id":"node0_001","level":0,"summary":"摘要还在","details":null,"profile":null,"meta":null}]
        """.trimIndent()
        val mapped = PcImport.map(
            ExportReader.read(json(nodes = nodes).toByteArray()),
            MemoryStore.DEFAULT_CHARACTER,
        )
        assertNull(mapped.nodes.single().details)
        assertTrue(
            "要提醒用户这次导出没带原文（否则他会以为记忆丢了）",
            mapped.skipped.any { it.contains("没有原文") },
        )
    }

    @Test
    fun `日历项没有日期时跳过而不是写一条坏数据`() = runBlocking {
        val calendar = """[{"id":9,"date":"","content":"坏数据"}]"""
        val mapped = PcImport.map(
            ExportReader.read(json(calendar = calendar).toByteArray()),
            MemoryStore.DEFAULT_CHARACTER,
        )
        assertTrue(mapped.calendar.isEmpty())
        assertTrue(mapped.skipped.any { it.contains("没有日期") })
    }
}
