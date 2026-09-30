package com.xiaozhu.planner.core.calendar

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZonedDateTime

/** 日期/时刻归一化与重复规则：纯函数，最容易出错也最该钉死。 */
class CalendarDatesTest {

    @Test
    fun `日期必须是严格的 yyyy-MM-dd`() {
        assertEquals("2026-09-30", CalendarDates.normDate("2026-09-30"))
        assertEquals("2026-09-30", CalendarDates.normDate(" 2026-09-30 "))
        assertNull(CalendarDates.normDate("2026-9-30"))       // 不补零 → 不接受
        assertNull(CalendarDates.normDate("明天"))            // 相对说法由模型算好
        assertNull(CalendarDates.normDate("2026-02-30"))      // 不存在的日期
        assertNull(CalendarDates.normDate(null))
    }

    @Test
    fun `时刻接受 9 05 并补零`() {
        assertEquals("09:05", CalendarDates.normTime("9:05"))
        assertEquals("20:00", CalendarDates.normTime("20:00"))
        assertEquals("00:00", CalendarDates.normTime("0:00"))
        assertNull(CalendarDates.normTime("24:00"))
        assertNull(CalendarDates.normTime("20:60"))
        assertNull(CalendarDates.normTime("8点"))
    }

    @Test
    fun `排序键把全天项排当天最前`() {
        assertTrue(CalendarDates.sortKey("2026-09-30", null) < CalendarDates.sortKey("2026-09-30", "09:00"))
        assertEquals("2026-09-30 00:00", CalendarDates.sortKey("2026-09-30", null))
    }

    @Test
    fun `到点键把全天项算作当天 23 59`() {
        // 与 PC 版 list_overdue_calendar 的 COALESCE(' 23:59') 一致：
        // 全天项不该在半夜 00:00 就被判为"已过点"
        assertEquals("2026-09-30 23:59", CalendarDates.dueKey("2026-09-30", null))
        assertEquals("2026-09-30 20:00", CalendarDates.dueKey("2026-09-30", "20:00"))
    }

    @Test
    fun `daily 每天都命中`() {
        val dates = CalendarDates.seriesDates("daily", "2026-09-28", "2026-10-01", "2026-09-28")
        assertEquals(listOf("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"), dates)
    }

    @Test
    fun `weekdays 跳过周末`() {
        // 2026-10-03 是周六、10-04 是周日
        val dates = CalendarDates.seriesDates("weekdays", "2026-10-02", "2026-10-05", "2026-10-02")
        assertEquals(listOf("2026-10-02", "2026-10-05"), dates)
    }

    @Test
    fun `weekly 跟的是基准日的星期几而不是续期起点`() {
        // 基准 2026-09-30 是周三；从 10-05（周一）开始续，只该命中 10-07（周三）
        val dates = CalendarDates.seriesDates("weekly", "2026-10-05", "2026-10-11", "2026-09-30")
        assertEquals(listOf("2026-10-07"), dates)
    }

    @Test
    fun `未知规则不生成任何日期`() {
        assertTrue(CalendarDates.seriesDates("monthly", "2026-09-01", "2026-12-01", "2026-09-01").isEmpty())
    }

    @Test
    fun `日期区间反了给空表`() {
        assertTrue(CalendarDates.seriesDates("daily", "2026-10-01", "2026-09-01", "2026-10-01").isEmpty())
    }

    @Test
    fun `时刻能转成 epoch 毫秒且用 UTC+8`() {
        val ms = CalendarDates.toEpochMillis("2026-09-30", "20:00")
        val back = java.time.Instant.ofEpochMilli(ms!!).atZone(CalendarDates.ZONE)
        assertEquals(2026, back.year)
        assertEquals(9, back.monthValue)
        assertEquals(30, back.dayOfMonth)
        assertEquals(20, back.hour)
        assertEquals(0, back.minute)
    }

    @Test
    fun `全天项不能转 epoch`() {
        assertNull(CalendarDates.toEpochMillis("2026-09-30", null))
        assertNull(CalendarDates.toEpochMillis("2026-9-30", "20:00"))
    }

    @Test
    fun `时长格式化`() {
        assertEquals("30 秒", CalendarDates.fmtDuration(0.5))
        assertEquals("15 分钟", CalendarDates.fmtDuration(15.0))
        assertEquals("1 小时", CalendarDates.fmtDuration(60.0))
        assertEquals("1.5 小时", CalendarDates.fmtDuration(90.0))
    }

    @Test
    fun `now 是 UTC+8`() {
        val now: ZonedDateTime = CalendarDates.now()
        assertEquals("Asia/Shanghai", now.zone.id)
    }
}
