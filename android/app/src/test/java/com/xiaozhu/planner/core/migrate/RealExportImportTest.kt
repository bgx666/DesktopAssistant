package com.xiaozhu.planner.core.migrate

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.agent.ConversationService
import com.xiaozhu.planner.core.memory.MemoryJson
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

/**
 * 用**真实导出文件**跑一遍完整导入。
 *
 * 为什么要有这个测试：`PcImportTest` 里的 JSON 是我按理解造的，它只能证明
 * "代码符合我的理解"。而真实数据里踩到过理解之外的形状（叶子 `details` 的
 * 两种形状、消息条目的两种形状），所以必须拿真文件过一遍。
 *
 * **不在仓库里放真实数据**（那是用户的私人对话）。文件由环境变量或约定路径提供，
 * 找不到就跳过——所以 CI 上是绿的、你本机想验就验：
 *
 * ```powershell
 * python scripts\export_to_android.py --memory-tree ... --calendar ... --out D:\Android\logs\planner-export.json
 * powershell -File build.ps1 -Task ":app:testDebugUnitTest"
 * ```
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class RealExportImportTest {

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

    private fun realExport(): File? {
        val candidates = listOfNotNull(
            System.getenv("PLANNER_ANDROID_EXPORT")?.takeIf { it.isNotBlank() }?.let(::File),
            File("D:/Android/logs/planner-export.json"),
            File(System.getProperty("user.home"), "planner-export.json"),
        )
        return candidates.firstOrNull { it.isFile && it.length() > 0 }
    }

    @Test
    fun `真实导出文件能完整导入，且一条记忆都不丢`() = runBlocking {
        val file = realExport()
        assumeTrue("没有真实导出文件，跳过（设 PLANNER_ANDROID_EXPORT 或放到 D:\\Android\\logs）", file != null)

        val payload = ExportReader.read(file!!.readBytes())
        val counts = payload.counts
        val importer = Importer(db)
        val preview = importer.preview(payload)
        val result = importer.apply(payload, wipeFirst = false)

        // 1) 数量对得上（导出端自报的 counts 是事实源）
        assertEquals("节点数应与导出文件一致", payload.nodes.size, result.nodes)
        assertEquals("日历项数应与导出文件一致", payload.calendarItems.size, result.calendar)
        if (counts != null) {
            assertEquals(counts.nodes, result.nodes)
            assertEquals(counts.calendarItems, result.calendar)
        }
        assertEquals("不应有跳过的条目：${result.warnings}", emptyList<String>(), result.warnings)

        val store = MemoryStore(db)
        assertEquals(payload.nodes.size, store.nodeCount())

        // 2) 每个叶子的原文都要在（这是"还记得细节"的来源）
        val leaves = payload.nodes.filter { it.level == 0 }
        val lostDetails = leaves.filter { node ->
            val entity = db.nodes().byId(MemoryStore.DEFAULT_CHARACTER, node.id)
            val details = MemoryJson.decodeDetails(entity?.details)
            details.messages.isEmpty()
        }
        assertTrue(
            "有 ${lostDetails.size}/${leaves.size} 个叶子丢了原文：" +
                lostDetails.take(3).map { it.id },
            lostDetails.isEmpty(),
        )

        // 3) 画像四维度不能丢
        val withProfile = payload.nodes.filter { it.profile != null }
        val lostProfile = withProfile.filter { node ->
            MemoryJson.decodeProfile(
                db.nodes().byId(MemoryStore.DEFAULT_CHARACTER, node.id)?.profile
            )?.isEmpty != false
        }
        assertTrue("有 ${lostProfile.size} 个节点的画像丢了", lostProfile.isEmpty())

        // 4) 层级与"活跃"标记要保住：
        //    active 的节点才会进上下文（被合并的子节点是 is_active=0），
        //    这个标记弄错的表现是模型看到重复摘要、或者该看到的没看到
        val activeByLevel = payload.nodes.filter { it.isActive != 0 }.groupBy { it.level }
        activeByLevel.forEach { (level, nodes) ->
            assertEquals("level $level 的活跃节点数", nodes.size, store.levelCount(level))
        }
        val inactive = payload.nodes.count { it.isActive == 0 }
        assertTrue("这份导出里应该有被合并过的节点（否则这个断言没在测东西）", inactive > 0)

        val parents = payload.nodes.filter { it.parentId != null }
        val brokenLinks = parents.filter { node ->
            db.nodes().byId(MemoryStore.DEFAULT_CHARACTER, node.id)?.parentId != node.parentId
        }
        assertTrue("有 ${brokenLinks.size} 个父子链接断了", brokenLinks.isEmpty())

        // 5) 真实内容能被检索到（"记得"要能被问出来）
        val sample = leaves.lastOrNull { node ->
            MemoryJson.decodeDetails(
                db.nodes().byId(MemoryStore.DEFAULT_CHARACTER, node.id)?.details
            ).messages.any { it.content.orEmpty().length >= 4 }
        }
        if (sample != null) {
            val details = MemoryJson.decodeDetails(
                db.nodes().byId(MemoryStore.DEFAULT_CHARACTER, sample.id)?.details
            )
            val word = details.messages.first { it.content.orEmpty().length >= 4 }.content.orEmpty().take(6)
            assertTrue("搜「$word」应该能命中导入的记忆", store.search(word).isNotEmpty())
        }

        // 6) 对话 buffer 恢复后必须是模型能吃的形状（工具调用成对）
        val history = ConversationService(store).loadHistory()
        assertEquals("buffer 条数应与导出一致", preview.bufferMessages, history.size)
        history.forEachIndexed { idx, msg ->
            assertTrue("第 $idx 条角色不合法：${msg.role}", msg.role in setOf("user", "assistant", "tool"))
            if (msg.role == "tool") {
                assertTrue("第 $idx 条 tool 消息没有 tool_call_id", !msg.toolCallId.isNullOrBlank())
            }
        }

        // 7) 日历：重复系列与状态都要保住
        val calendar = db.calendar().rangeAll("0000-01-01", "9999-12-31")
        assertEquals(result.calendar, calendar.size)
        val exportedSeries = payload.calendarItems.mapNotNull { it.seriesId }.toSet()
        val importedSeries = calendar.mapNotNull { it.seriesId }.toSet()
        assertEquals("重复系列的 id 应原样保住", exportedSeries, importedSeries)
        val statuses = payload.calendarItems.groupingBy { it.status }.eachCount()
        statuses.forEach { (status, expected) ->
            assertEquals("status=$status 的项数", expected, calendar.count { it.status == status })
        }

        println(
            "[real-export] 导入完成：节点 ${result.nodes}（叶子 ${leaves.size}）、" +
                "对话 ${result.messages}、日程 ${result.calendar}、系列 ${importedSeries.size}"
        )
    }

    @Test
    fun `真实导出文件先预览再导入时，预览数字与结果一致`() = runBlocking {
        val file = realExport()
        assumeTrue("没有真实导出文件，跳过", file != null)

        val payload = ExportReader.read(file!!.readBytes())
        val importer = Importer(db)
        val preview = importer.preview(payload)
        val result = importer.apply(payload, wipeFirst = false)

        assertEquals(preview.nodes, result.nodes)
        assertEquals(preview.calendarItems, result.calendar)
        assertEquals(preview.bufferMessages, result.messages)
        assertTrue("第一次导入前本地应该是空的", !preview.hasLocalData)
    }
}
