package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.bool
import com.xiaozhu.planner.core.agent.int
import com.xiaozhu.planner.core.agent.long
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.core.calendar.DndSettings
import com.xiaozhu.planner.core.calendar.DndStore
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZonedDateTime

/**
 * 参数容错：模型给的类型经常不对（true 写成 "true"、id 写成 "3"），
 * 这里定下"能救就救、救不了落到默认值"的边界，免得每处再各写一遍。
 */
class ToolArgsTest {

    private fun obj(vararg pairs: Pair<String, Any>): JsonObject = buildJsonObject {
        pairs.forEach { (k, v) ->
            when (v) {
                is Int -> put(k, v)
                is Long -> put(k, v)
                is Boolean -> put(k, v)
                is Double -> put(k, v)
                else -> put(k, v.toString())
            }
        }
    }

    @Test
    fun `bool 认识常见的几种写法`() {
        assertTrue(obj("x" to true).bool("x"))
        assertTrue(obj("x" to "true").bool("x"))
        assertTrue(obj("x" to "TRUE").bool("x"))
        assertTrue(obj("x" to "1").bool("x"))
        assertTrue(obj("x" to "yes").bool("x"))
        assertTrue(obj("x" to "是").bool("x"))
        assertFalse(obj("x" to false).bool("x"))
        assertFalse(obj("x" to "false").bool("x"))
        assertFalse(obj("x" to "0").bool("x"))
        assertFalse(obj("x" to "").bool("x"))
    }

    @Test
    fun `bool 遇到看不懂的值落到默认值`() {
        assertFalse(obj("x" to "随便").bool("x"))
        assertTrue(obj("x" to "随便").bool("x", default = true))
        assertTrue(obj().bool("x", default = true))
    }

    @Test
    fun `long 与 int 能吃字符串数字`() {
        assertEquals(7L, obj("x" to 7).long("x", 0L))
        assertEquals(7L, obj("x" to "7").long("x", 0L))
        assertEquals(0L, obj("x" to "七").long("x", 0L))
        assertEquals(3, obj("x" to "3").int("x", 0))
        assertEquals(0, obj("x" to 3.5).int("x", 0))
    }

    @Test
    fun `str 把空白当作没给，数字会被当成字符串`() {
        assertEquals("a", obj("x" to "a").str("x"))
        assertNull(obj("x" to "  ").str("x"))
        assertEquals("12", obj("x" to 12).str("x"))
    }
}

/** 免打扰工具：开 / 关 / 指定钟点。 */
class SetDoNotDisturbToolTest {

    private class FakeStore : DndStore {
        var saved: DndSettings? = null
        override fun load(): DndSettings = saved ?: DndSettings()
        override fun save(settings: DndSettings) { saved = settings }
    }

    private val at15: () -> ZonedDateTime =
        { ZonedDateTime.of(2026, 9, 30, 15, 0, 0, 0, CalendarDates.ZONE) }

    @Test
    fun `开启时不带钟点就说明用默认窗口`() = runBlocking {
        val store = FakeStore()
        val out = SetDoNotDisturbTool(store, at15).run(
            buildJsonObject { put("enabled", true) }
        )
        assertTrue(out, out.contains("已开启免打扰"))
        assertTrue(out.contains("22:00-8:00") || out.contains("默认"))
        assertEquals(true, store.saved?.enabled)
        assertNull(store.saved?.untilEpochMillis)
    }

    @Test
    fun `开启并指定钟点时算出截止时刻`() = runBlocking {
        val store = FakeStore()
        val out = SetDoNotDisturbTool(store, at15).run(
            buildJsonObject { put("enabled", true); put("until_hour", 18) }
        )
        assertTrue(out.contains("直到"))
        val until = store.saved!!.untilEpochMillis!!
        val back = java.time.Instant.ofEpochMilli(until).atZone(CalendarDates.ZONE)
        assertEquals(18, back.hour)
        assertEquals(30, back.dayOfMonth)   // 15:00 说"直到 18 点" ＝ 今天
    }

    @Test
    fun `关掉时清空截止时刻`() = runBlocking {
        val store = FakeStore()
        store.save(DndSettings(enabled = true, untilEpochMillis = 1L))
        SetDoNotDisturbTool(store, at15).run(buildJsonObject { put("enabled", false) })
        assertEquals(false, store.saved?.enabled)
        assertNull(store.saved?.untilEpochMillis)
    }

    @Test
    fun `钟点越界时明确报错且不写状态`() = runBlocking {
        val store = FakeStore()
        val out = SetDoNotDisturbTool(store, at15).run(
            buildJsonObject { put("enabled", true); put("until_hour", 25) }
        )
        assertTrue(out, out.contains("0-23"))
        assertNull(store.saved)
    }

    @Test
    fun `指定已过的钟点顺延到明天`() = runBlocking {
        val store = FakeStore()
        SetDoNotDisturbTool(store, at15).run(
            buildJsonObject { put("enabled", true); put("until_hour", 8) }
        )
        val back = java.time.Instant.ofEpochMilli(store.saved!!.untilEpochMillis!!)
            .atZone(CalendarDates.ZONE)
        assertEquals(10, back.monthValue)   // 9-30 15:00 → 10-1 08:00
        assertEquals(1, back.dayOfMonth)
    }

    @Test
    fun `工具名与 prompt 里写的一致`() {
        val tool = SetDoNotDisturbTool(FakeStore(), at15)
        assertEquals("set_do_not_disturb", tool.name)
    }

    @Test
    fun `开启后 Dnd 判定立刻生效`() = runBlocking {
        val store = FakeStore()
        val now = at15()
        // 白天本来不该免打扰
        assertFalse(Dnd.inDnd(now, store.load()))
        SetDoNotDisturbTool(store, at15).run(
            buildJsonObject { put("enabled", true); put("until_hour", 18) }
        )
        assertTrue(Dnd.inDnd(now, store.load()))
    }
}
