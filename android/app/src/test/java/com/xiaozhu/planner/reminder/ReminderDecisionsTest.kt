package com.xiaozhu.planner.reminder

import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.data.db.CalendarItemEntity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 闹钟触发后的决策。这一组全是"差一点就错"的边界：陈旧闹钟、免打扰顺延、
 * 已完成/已取消。错了的表现是"提醒没响"或"响错时间"，真机上极难复盘，
 * 所以全部钉在这里。
 */
class ReminderDecisionsTest {

    private val at20 = CalendarDates.toEpochMillis("2026-09-30", "20:00")!!
    private val at21 = CalendarDates.toEpochMillis("2026-09-30", "21:00")!!

    private fun item(
        date: String = "2026-09-30",
        time: String? = "20:00",
        status: String = CalendarItemEntity.STATUS_TODO,
        content: String = "吃药",
    ) = CalendarItemEntity(id = 1, date = date, time = time, content = content, status = status)

    @Test
    fun `正常到点就投递`() {
        val d = ReminderDecisions.decide(
            item = item(), scheduledAt = at20, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20,
        )
        assertTrue(d is ReminderDecision.Deliver)
        assertEquals("「吃药」（原定 2026-09-30 20:00）", (d as ReminderDecision.Deliver).line)
    }

    @Test
    fun `Doze 晚触发也算正常投递`() {
        // 闹钟晚了 20 分钟响：AT 与 item 时刻一致，就该照常提醒（不能因为迟到就不提）
        val d = ReminderDecisions.decide(
            item = item(), scheduledAt = at20, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20 + 20 * 60_000,
        )
        assertTrue(d is ReminderDecision.Deliver)
    }

    @Test
    fun `陈旧闹钟按新时刻重排，不投递也不标已提醒`() {
        // 改到 21:00 之后，20:00 的旧闹钟仍可能触发（进程重启后没被 cancel 掉）
        val d = ReminderDecisions.decide(
            item = item(time = "21:00"), scheduledAt = at20, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20,
        )
        assertEquals(ReminderDecision.Reschedule(at21, deferred = false), d)
    }

    @Test
    fun `陈旧闹钟的新时刻也过去了就不投递`() {
        // 交给"补提醒"路径，避免这里重复弹一条
        val d = ReminderDecisions.decide(
            item = item(time = "21:00"), scheduledAt = at20, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at21 + 60_000,
        )
        assertEquals(ReminderDecision.Skip, d)
    }

    @Test
    fun `免打扰顺延到窗口结束，且标记为顺延`() {
        val end = at21
        val d = ReminderDecisions.decide(
            item = item(), scheduledAt = at20, deferred = false,
            inDnd = true, dndEndMillis = end, nowMillis = at20,
        )
        assertEquals(ReminderDecision.Reschedule(end, deferred = true), d)
    }

    @Test
    fun `顺延过来的闹钟不再做陈旧校验`() {
        // 顺延后的 AT 本来就 ≠ item 的时刻；若还按陈旧处理会无限顺延下去
        val end = at21
        val d = ReminderDecisions.decide(
            item = item(), scheduledAt = end, deferred = true,
            inDnd = false, dndEndMillis = 0L, nowMillis = end,
        )
        assertTrue("顺延后到点必须真的投递", d is ReminderDecision.Deliver)
    }

    @Test
    fun `免打扰已结束但闹钟又响了：按普通闹钟处理`() {
        val d = ReminderDecisions.decide(
            item = item(), scheduledAt = at20, deferred = true,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20,
        )
        assertTrue(d is ReminderDecision.Deliver)
    }

    @Test
    fun `已完成或已取消的不投递`() {
        listOf(CalendarItemEntity.STATUS_DONE, CalendarItemEntity.STATUS_CANCELLED).forEach { st ->
            val d = ReminderDecisions.decide(
                item = item(status = st), scheduledAt = at20, deferred = false,
                inDnd = false, dndEndMillis = 0L, nowMillis = at20,
            )
            assertEquals("$st 不该再提醒", ReminderDecision.Skip, d)
        }
    }

    @Test
    fun `项已被删掉就什么都不做`() {
        val d = ReminderDecisions.decide(
            item = null, scheduledAt = at20, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20,
        )
        assertEquals(ReminderDecision.Skip, d)
    }

    @Test
    fun `全天项没有到点闹钟，真响了也按陈旧处理`() {
        // expected = null → 不做陈旧校验，但如果它真的响了，至少不该崩
        val d = ReminderDecisions.decide(
            item = item(time = null), scheduledAt = at20, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20,
        )
        assertTrue(d is ReminderDecision.Deliver)
    }

    @Test
    fun `scheduledAt 未知时按可信处理`() {
        val d = ReminderDecisions.decide(
            item = item(time = "21:00"), scheduledAt = 0L, deferred = false,
            inDnd = false, dndEndMillis = 0L, nowMillis = at20,
        )
        assertTrue("拿不到排定时刻就别自作聪明地跳过", d is ReminderDecision.Deliver)
    }

    @Test
    fun `免打扰顺延时刻已过则跳过`() {
        val d = ReminderDecisions.decide(
            item = item(), scheduledAt = at20, deferred = false,
            inDnd = true, dndEndMillis = at20 - 1000, nowMillis = at20,
        )
        assertEquals(ReminderDecision.Skip, d)
    }
}
