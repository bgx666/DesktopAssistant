package com.xiaozhu.planner.core.tools

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.ReminderPlanner
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
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
 * 日历工具的对外行为：模型传什么就发生什么、传错话给回可说人话的错误，
 * 以及**每次写入之后都要求重排闹钟**（漏了这次调用，提醒就永远不会响）。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CalendarToolsTest {

    private lateinit var db: PlannerDatabase
    private lateinit var store: CalendarStore

    private var syncCount = 0
    private val planner = object : ReminderPlanner {
        override suspend fun sync() { syncCount++ }
    }

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        store = CalendarStore(db.calendar())
        syncCount = 0
    }

    @After
    fun tearDown() = db.close()

    private fun args(vararg pairs: Pair<String, Any>): JsonObject = buildJsonObject {
        pairs.forEach { (k, v) ->
            when (v) {
                is Int -> put(k, v)
                is Long -> put(k, v)
                is Boolean -> put(k, v)
                else -> put(k, v.toString())
            }
        }
    }

    @Test
    fun `加一条带时刻的日程，并说明到点会弹提醒`() = runBlocking {
        val out = AddScheduleTool(store, planner).run(
            args("date" to "2026-09-30", "content" to "复习线代第三章", "time" to "20:00")
        )
        assertTrue(out, out.contains("已加入日历 #1"))
        assertTrue(out.contains("20:00"))
        assertTrue(out.contains("到点手机会弹提醒"))

        val item = store.day("2026-09-30").single()
        assertEquals("复习线代第三章", item.content)
        assertEquals("20:00", item.time)
        assertEquals(CalendarItemEntity.KIND_EVENT, item.kind)
        assertEquals(1, syncCount)
    }

    @Test
    fun `时刻格式非法时明确报错且不写库`() = runBlocking {
        val out = AddScheduleTool(store, planner).run(
            args("date" to "2026-09-30", "content" to "开会", "time" to "晚上八点")
        )
        assertTrue(out.contains("时刻格式应为 HH:MM"))
        assertTrue(store.day("2026-09-30").isEmpty())
        assertEquals("没写库就不该重排闹钟", 0, syncCount)
    }

    @Test
    fun `日期非法时明确报错`() = runBlocking {
        val out = AddScheduleTool(store, planner).run(
            args("date" to "明天", "content" to "开会")
        )
        assertTrue(out.contains("日期格式应为 YYYY-MM-DD"))
        assertEquals(0, syncCount)
    }

    @Test
    fun `全天项不承诺到点提醒`() = runBlocking {
        val out = AddScheduleTool(store, planner).run(
            args("date" to "2026-09-30", "content" to "交作业")
        )
        assertTrue(out.contains("（全天）"))
        assertTrue("全天项没有到点提醒，别对用户承诺", !out.contains("到点手机会弹提醒"))
        assertNull(store.day("2026-09-30").single().time)
    }

    @Test
    fun `repeat 走系列并报告排了多少期`() = runBlocking {
        val out = AddScheduleTool(store, planner).run(
            args(
                "date" to "2026-09-28", "content" to "吃药", "time" to "08:00",
                "repeat" to "daily",
            )
        )
        assertTrue(out, out.contains("每天"))
        val sid = store.seriesSummary().single().seriesId
        val items = store.seriesItems(sid)
        assertEquals(CalendarStore.REPEAT_HORIZON_DAYS.toInt() + 1, items.size)
    }

    @Test
    fun `非法 repeat 被挡下`() = runBlocking {
        val out = AddScheduleTool(store, planner).run(
            args("date" to "2026-09-28", "content" to "吃药", "repeat" to "monthly")
        )
        assertTrue(out.contains("repeat 只能是 daily/weekly/weekdays"))
        assertEquals(0, syncCount)
    }

    @Test
    fun `列表按区间给出行和已完成标记`() = runBlocking {
        val add = AddScheduleTool(store, planner)
        add.run(args("date" to "2026-09-30", "content" to "复习", "time" to "20:00"))
        add.run(args("date" to "2026-10-01", "content" to "交作业"))
        val id = store.day("2026-09-30").single().id
        UpdateScheduleTool(store, planner).run(args("item_id" to id, "status" to "done"))

        val out = ListScheduleTool(store).run(
            args("start_date" to "2026-09-30", "end_date" to "2026-10-01")
        )
        assertTrue(out, out.contains("共 2 项"))
        assertTrue(out.contains("[已完成]"))
        assertTrue(out, out.contains("2026-10-01 全天 [日程] 交作业"))
    }

    @Test
    fun `单日查询留空 end_date`() = runBlocking {
        AddScheduleTool(store, planner).run(args("date" to "2026-09-30", "content" to "今天的事"))
        AddScheduleTool(store, planner).run(args("date" to "2026-10-05", "content" to "以后的事"))
        val out = ListScheduleTool(store).run(args("start_date" to "2026-09-30"))
        assertTrue(out.contains("今天的事"))
        assertTrue(!out.contains("以后的事"))
    }

    @Test
    fun `空区间给一句明确的话`() = runBlocking {
        val out = ListScheduleTool(store).run(args("start_date" to "2026-09-30"))
        assertTrue(out.contains("没有任何日程或待办"))
    }

    @Test
    fun `改时刻后要重排闹钟`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-30", "content" to "打电话", "time" to "20:00")
        )
        syncCount = 0
        val id = store.day("2026-09-30").single().id
        val out = UpdateScheduleTool(store, planner).run(args("item_id" to id, "time" to "21:30"))
        assertTrue(out.contains("已更新日历项 #$id"))
        assertEquals("21:30", store.byId(id)!!.time)
        assertEquals(1, syncCount)
    }

    @Test
    fun `传全天表示去掉时刻`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-30", "content" to "打电话", "time" to "20:00")
        )
        val id = store.day("2026-09-30").single().id
        UpdateScheduleTool(store, planner).run(args("item_id" to id, "time" to "全天"))
        assertNull(store.byId(id)!!.time)
    }

    @Test
    fun `没有要改的字段时明确说清楚`() = runBlocking {
        AddScheduleTool(store, planner).run(args("date" to "2026-09-30", "content" to "打电话"))
        val id = store.day("2026-09-30").single().id
        val out = UpdateScheduleTool(store, planner).run(args("item_id" to id))
        assertTrue(out.contains("没有要修改的字段"))
    }

    @Test
    fun `找不到的 id 给提示而不是抛异常`() = runBlocking {
        val out = UpdateScheduleTool(store, planner).run(args("item_id" to 999))
        assertTrue(out.contains("找不到日历项 #999"))
        assertTrue(CancelScheduleTool(store, planner).run(args("item_id" to 999)).contains("找不到"))
    }

    @Test
    fun `status 只接受 todo 和 done`() = runBlocking {
        AddScheduleTool(store, planner).run(args("date" to "2026-09-30", "content" to "打电话"))
        val id = store.day("2026-09-30").single().id
        val out = UpdateScheduleTool(store, planner).run(args("item_id" to id, "status" to "cancelled"))
        assertTrue(out.contains("取消请用 cancel_schedule"))
    }

    @Test
    fun `取消单次不动系列其余各期`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-28", "content" to "吃药", "time" to "08:00", "repeat" to "daily")
        )
        val sid = store.seriesSummary().single().seriesId
        val first = store.seriesItems(sid).first()
        syncCount = 0
        val out = CancelScheduleTool(store, planner).run(args("item_id" to first.id))
        assertTrue(out.contains("今后各期仍在"))
        assertEquals(1, syncCount)
        assertEquals(CalendarItemEntity.STATUS_CANCELLED, store.byId(first.id)!!.status)
        assertEquals(CalendarItemEntity.STATUS_TODO, store.seriesItems(sid)[1].status)
    }

    @Test
    fun `whole_series 取消系列今后未完成的`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-28", "content" to "吃药", "time" to "08:00", "repeat" to "daily")
        )
        val sid = store.seriesSummary().single().seriesId
        val first = store.seriesItems(sid).first()
        val out = CancelScheduleTool(store, planner).run(
            args("item_id" to first.id, "whole_series" to true)
        )
        assertTrue(out, out.contains("已取消重复项"))
        assertTrue(store.seriesItems(sid).all { it.status == CalendarItemEntity.STATUS_CANCELLED })
    }

    @Test
    fun `whole_series 改时刻会改到今后所有未完成的期`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-28", "content" to "吃药", "time" to "08:00", "repeat" to "daily"),
        )
        val sid = store.seriesSummary().single().seriesId
        val first = store.seriesItems(sid).first()
        syncCount = 0
        val out = UpdateScheduleTool(store, planner).run(
            args("item_id" to first.id, "time" to "09:00", "whole_series" to true)
        )
        assertTrue(out, out.contains("已更新重复系列"))
        assertTrue(store.seriesItems(sid).all { it.time == "09:00" })
        assertEquals(1, syncCount)
    }

    @Test
    fun `whole_series 改日期被拒绝并说清原因`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-28", "content" to "吃药", "repeat" to "daily")
        )
        val sid = store.seriesSummary().single().seriesId
        val first = store.seriesItems(sid).first()
        val out = UpdateScheduleTool(store, planner).run(
            args("item_id" to first.id, "date" to "2026-10-01", "whole_series" to true)
        )
        assertTrue(out, out.contains("date/status 只能改这一次"))
        assertEquals("2026-09-28", store.byId(first.id)!!.date)
    }

    @Test
    fun `列表把重复项标出来供模型判断这一次还是整个系列`() = runBlocking {
        AddScheduleTool(store, planner).run(
            args("date" to "2026-09-30", "content" to "周会", "time" to "10:00", "repeat" to "weekly")
        )
        val out = ListScheduleTool(store).run(args("start_date" to "2026-09-30"))
        assertTrue(out, out.contains("↻每周"))
    }

    @Test
    fun `秒级 heartbeat 不受日历影响`() = runBlocking {
        // 回归用：日历工具注册后不能把 heartbeat 的语义带歪
        AddScheduleTool(store, planner).run(args("date" to "2026-09-30", "content" to "x", "time" to "20:00"))
        assertNotNull(store.day("2026-09-30").single().time)
    }
}
