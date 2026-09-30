package com.xiaozhu.planner.core.calendar

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.data.db.CalendarItemEntity
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
 * 日历库测试（Robolectric + 真 SQLite）。覆盖 PC 版 `calendar_db.py` 的全部语义：
 * 区间查询、软取消、重复系列生成/续期/批量改/批量取消、到点与逾期判据。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CalendarStoreTest {

    private lateinit var db: PlannerDatabase
    private lateinit var store: CalendarStore

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        db = Room.inMemoryDatabaseBuilder(ctx, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        store = CalendarStore(db.calendar())
    }

    @After
    fun tearDown() = db.close()

    @Test
    fun `加一项并按区间查回来，全天项排当天最前`() = runBlocking {
        store.add("2026-09-30", "复习线代", "20:00")
        store.add("2026-09-30", "交作业")
        store.add("2026-10-01", "见导师", "09:00")

        val day = store.day("2026-09-30")
        assertEquals(listOf("交作业", "复习线代"), day.map { it.content })

        val range = store.range("2026-09-30", "2026-10-01")
        assertEquals(3, range.size)
        assertEquals("2026-09-30", range.first().date)
    }

    @Test
    fun `取消是软取消：区间查询不带它，rangeAll 还看得见`() = runBlocking {
        val id = store.add("2026-09-30", "不去了", "18:00")
        assertTrue(store.cancel(id))

        assertTrue(store.range("2026-09-30", "2026-09-30").isEmpty())
        val all = store.rangeAll("2026-09-30", "2026-09-30")
        assertEquals(1, all.size)
        assertEquals(CalendarItemEntity.STATUS_CANCELLED, all.first().status)
    }

    @Test
    fun `标完成写 doneAt，撤销完成清空`() = runBlocking {
        val id = store.add("2026-09-30", "写完", "10:00")
        store.setStatus(id, CalendarItemEntity.STATUS_DONE)
        assertNotNull(store.byId(id)!!.doneAt)

        store.setStatus(id, CalendarItemEntity.STATUS_TODO)
        assertNull(store.byId(id)!!.doneAt)
    }

    @Test
    fun `改期或改时刻会清掉提醒标记`() = runBlocking {
        val id = store.add("2026-09-30", "打电话", "20:00")
        store.markReminded(id, "2026-09-30 20:00")
        assertNotNull(store.byId(id)!!.remindedAt)

        store.update(id = id, time = "21:00")
        assertNull("改到未来却还留着已提醒标记，就再也不会提醒了", store.byId(id)!!.remindedAt)
    }

    @Test
    fun `daily 系列一次生成多期并共享 seriesId`() = runBlocking {
        val (sid, count) = store.addSeries("2026-09-28", "吃药", "daily", "08:00", horizonDays = 3)
        assertEquals(4, count)     // 9-28 起 3 天 = 4 期（闭区间）
        assertTrue(sid.isNotEmpty())

        val items = store.seriesItems(sid)
        assertEquals(4, items.size)
        assertTrue(items.all { it.repeat == "daily" && it.repeatUntil == "2026-10-01" })

        val summary = store.seriesSummary().single()
        assertEquals(4, summary.total)
        assertEquals(4, summary.todoCount)
    }

    @Test
    fun `续期从最后一天接着排，且 weekly 不漂星期几`() = runBlocking {
        // 基准 2026-09-30 是周三
        val (sid, _) = store.addSeries("2026-09-30", "周会", "weekly", "10:00", horizonDays = 7)
        assertEquals(listOf("2026-09-30", "2026-10-07"), store.seriesItems(sid).map { it.date })

        // 站在 10-01 续 14 天 → 目标 10-15，接着排 10-14
        val added = store.extendSeries(sid, today = "2026-10-01", horizonDays = 14)
        assertEquals(1, added)
        assertEquals(
            listOf("2026-09-30", "2026-10-07", "2026-10-14"),
            store.seriesItems(sid).map { it.date },
        )
    }

    @Test
    fun `快排完的系列会被列入待续期`() = runBlocking {
        store.addSeries("2026-09-28", "吃药", "daily", "08:00", horizonDays = 3)
        // 已生成到 10-01；站在 09-28 看，离排完还有 3 天 ≤ 60 → 要续
        assertEquals(1, store.seriesNeedExtend("2026-09-28").size)
        // 站在 09-27 看也一样（阈值内）
        assertEquals(1, store.seriesNeedExtend("2026-09-20").size)
    }

    @Test
    fun `批量改系列不动已完成的行`() = runBlocking {
        val (sid, _) = store.addSeries("2026-09-28", "吃药", "daily", "08:00", horizonDays = 3)
        val items = store.seriesItems(sid)
        store.setStatus(items.first().id, CalendarItemEntity.STATUS_DONE)

        val n = store.updateSeries(sid, fromDate = "2026-09-28", time = "09:00",
            kind = CalendarItemEntity.KIND_EVENT, content = "吃药（改）", detail = "")
        assertEquals(3, n)     // 4 期里 1 期已完成，不动

        val after = store.seriesItems(sid)
        assertEquals("08:00", after.first().time)          // 已完成那期保持不变
        assertTrue(after.drop(1).all { it.time == "09:00" && it.content == "吃药（改）" })
    }

    @Test
    fun `批量取消系列只影响今后未完成的`() = runBlocking {
        val (sid, _) = store.addSeries("2026-09-28", "吃药", "daily", "08:00", horizonDays = 3)
        val items = store.seriesItems(sid)
        store.setStatus(items.first().id, CalendarItemEntity.STATUS_DONE)

        val n = store.cancelSeries(sid, fromDate = "2026-09-29")
        assertEquals(3, n)
        val after = store.seriesItems(sid)
        assertEquals(CalendarItemEntity.STATUS_DONE, after.first().status)
        assertTrue(after.drop(1).all { it.status == CalendarItemEntity.STATUS_CANCELLED })
    }

    @Test
    fun `到点判据只看带时刻的待办项`() = runBlocking {
        store.add("2026-09-30", "全天的事")                    // 全天 → 不参与到点
        store.add("2026-09-30", "到点了", "20:00")
        store.add("2026-09-30", "还没到", "21:00")
        val done = store.add("2026-09-30", "已完成的", "19:00")
        store.setStatus(done, CalendarItemEntity.STATUS_DONE)

        val due = store.due("2026-09-30 20:30")
        assertEquals(listOf("到点了"), due.map { it.content })
    }

    @Test
    fun `已提醒过的不再重复投递`() = runBlocking {
        val id = store.add("2026-09-30", "到点了", "20:00")
        store.markReminded(id)
        assertTrue(store.due("2026-09-30 20:30").isEmpty())
    }

    @Test
    fun `逾期判据把全天项算作当天 23 59`() = runBlocking {
        store.add("2026-09-29", "昨天没做的全天项")
        store.add("2026-09-30", "今天的全天项")
        val overdue = store.overdue("2026-09-30 10:00")
        assertEquals(listOf("昨天没做的全天项"), overdue.map { it.content })
    }

    @Test
    fun `未来的待办项用于重排闹钟`() = runBlocking {
        store.add("2026-09-30", "今天稍后", "23:00")
        store.add("2026-10-02", "后天", "09:00")
        val future = store.upcoming("2026-09-30")
        assertEquals(2, future.size)

        val id = future.first().id
        store.markReminded(id)
        assertEquals(1, store.upcoming("2026-09-30").size)
    }

    @Test
    fun `非法重复规则是编程错误，直接抛而不是静默生成空系列`() = runBlocking {
        val failed = runCatching { store.addSeries("2026-09-01", "x", "monthly") }.isFailure
        assertTrue("规则白名单是工具层挡的，store 这层必须响", failed)
    }

    @Test
    fun `起始日非法时系列不生成任何行`() = runBlocking {
        val (sid, count) = store.addSeries("2026-9-1", "x", "daily")
        assertTrue(sid.isEmpty())
        assertEquals(0, count)
        assertTrue(store.rangeAll("2026-01-01", "2027-01-01").isEmpty())
    }
}
