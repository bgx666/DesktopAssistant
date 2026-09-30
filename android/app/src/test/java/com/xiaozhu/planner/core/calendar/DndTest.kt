package com.xiaozhu.planner.core.calendar

import com.xiaozhu.planner.data.db.CalendarItemEntity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZonedDateTime

/** 免打扰窗口（含跨天的 22–8）——纯函数，边界最多也最容易写错。 */
class DndTest {

    private fun at(hour: Int, minute: Int = 0): ZonedDateTime =
        ZonedDateTime.of(2026, 9, 30, hour, minute, 0, 0, CalendarDates.ZONE)

    @Test
    fun `默认窗口 22 到 8 之间为免打扰`() {
        val on = DndSettings(enabled = true)
        assertTrue(Dnd.inDnd(at(22), on))
        assertTrue(Dnd.inDnd(at(23, 59), on))
        assertTrue(Dnd.inDnd(at(0), on))
        assertTrue(Dnd.inDnd(at(7, 59), on))
        assertFalse(Dnd.inDnd(at(8), on))
        assertFalse(Dnd.inDnd(at(12), on))
        assertFalse(Dnd.inDnd(at(21, 59), on))
    }

    @Test
    fun `关掉开关后夜间也不再免打扰`() {
        assertFalse(Dnd.inDnd(at(23), DndSettings(enabled = false)))
    }

    @Test
    fun `显式截止时刻优先于开关，到点自动恢复`() {
        val now = at(15)
        val until = at(16).toInstant().toEpochMilli()
        // 白天本来不在默认窗口里，但显式开了"直到 16:00"就该生效
        assertTrue(Dnd.inDnd(now, DndSettings(enabled = true, untilEpochMillis = until)))
        assertFalse(Dnd.inDnd(at(17), DndSettings(enabled = true, untilEpochMillis = until)))
    }

    @Test
    fun `显式截止已过期时回落到默认窗口判断`() {
        val past = at(1).toInstant().toEpochMilli()
        // 01:00 的截止早就过了；现在 23:00 仍在默认窗口内 → 仍是免打扰
        assertTrue(Dnd.inDnd(at(23), DndSettings(enabled = true, untilEpochMillis = past)))
        // 现在 12:00 不在默认窗口 → 不是
        assertFalse(Dnd.inDnd(at(12), DndSettings(enabled = true, untilEpochMillis = past)))
    }

    @Test
    fun `untilHour 已过的钟点顺延到明天`() {
        val now = at(23, 30)
        val until = Dnd.untilHour(now, 8)
        val back = java.time.Instant.ofEpochMilli(until).atZone(CalendarDates.ZONE)
        assertEquals(10, back.monthValue)   // 9-30 → 10-1
        assertEquals(1, back.dayOfMonth)
        assertEquals(8, back.hour)
        assertEquals(0, back.minute)
    }

    @Test
    fun `untilHour 未到的钟点就是今天`() {
        val until = Dnd.untilHour(at(8), 20)
        val back = java.time.Instant.ofEpochMilli(until).atZone(CalendarDates.ZONE)
        assertEquals(30, back.dayOfMonth)
        assertEquals(20, back.hour)
    }

    @Test
    fun `非跨天窗口也能算`() {
        // 自定义成 13:00-15:00（不跨天）
        assertTrue(Dnd.inDnd(at(14), DndSettings(enabled = true), startHour = 13, endHour = 15))
        assertFalse(Dnd.inDnd(at(15), DndSettings(enabled = true), startHour = 13, endHour = 15))
        assertFalse(Dnd.inDnd(at(3), DndSettings(enabled = true), startHour = 13, endHour = 15))
    }
}

/** 议程快照：模型每轮看到的那段文本。 */
class AgendaSnapshotTest {

    private var nextId = 1L

    private fun item(
        date: String,
        time: String?,
        content: String,
        status: String = CalendarItemEntity.STATUS_TODO,
        seriesId: String? = null,
        repeat: String? = null,
    ) = CalendarItemEntity(
        id = nextId++,
        date = date,
        time = time,
        content = content,
        status = status,
        seriesId = seriesId,
        repeat = repeat,
    )

    @Test
    fun `空日历也要给一句明确的话`() {
        val text = AgendaSnapshot.render("2026-09-30", emptyList(), "10:00")
        assertTrue(text.startsWith("[当前议程]（2026-09-30，10:00）"))
        assertTrue(text.contains("还是空的"))
    }

    @Test
    fun `列出未完成项并标出 id 与时刻`() {
        val text = AgendaSnapshot.render(
            "2026-09-30",
            listOf(item("2026-09-30", "20:00", "复习线代"), item("2026-10-01", null, "交作业")),
            "09:00",
        )
        assertTrue(text.contains("共 2 项，其中未完成 2 项"))
        assertTrue(text.contains("#1 2026-09-30 20:00 复习线代"))
        assertTrue(text.contains("#2 2026-10-01 全天 交作业"))
    }

    @Test
    fun `重复项折叠成一行，不把窗口占满`() {
        // 每天吃药：窗口内 8 期，只该出现一行，并注明还有几期
        val items = (28..30).map { item("2026-09-$it", "08:00", "吃药", seriesId = "s1", repeat = "daily") } +
            item("2026-09-30", "20:00", "复习线代")
        val text = AgendaSnapshot.render("2026-09-30", items, "09:00")

        assertEquals(1, Regex("吃药").findAll(text).count())
        assertTrue(text.contains("（每天，本窗口还有 3 期）"))
        assertTrue(text.contains("复习线代"))
    }

    @Test
    fun `全部完成时明确说完成`() {
        val text = AgendaSnapshot.render(
            "2026-09-30",
            listOf(item("2026-09-30", "20:00", "做完了", status = CalendarItemEntity.STATUS_DONE)),
            "09:00",
        )
        assertTrue(text.contains("全部已完成"))
    }

    @Test
    fun `超过十行时提示去看 list_schedule`() {
        val items = (1..13).map { item("2026-10-%02d".format(it), "09:00", "事项$it") }
        val text = AgendaSnapshot.render("2026-10-01", items, "09:00")
        assertTrue(text.contains("…另有 3 项（用 list_schedule 细看）"))
    }

    @Test
    fun `指纹只跟可变字段有关`() {
        val a = item("2026-09-30", "20:00", "复习", seriesId = null)
        val same = a.copy()
        assertFalse(AgendaSnapshot.fingerprint(listOf(a)) == AgendaSnapshot.fingerprint(listOf(same.copy(status = CalendarItemEntity.STATUS_DONE))))
        assertTrue(AgendaSnapshot.fingerprint(listOf(a)) == AgendaSnapshot.fingerprint(listOf(same.copy(content = "改了内容"))))
    }
}
