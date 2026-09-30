package com.xiaozhu.planner.core.tools

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

class HeartbeatToolTest {

    private class FakeStore : HeartbeatStore {
        var at: Long? = null
        var note: String = ""
        override fun setNextWake(atMillis: Long, note: String) {
            at = atMillis; this.note = note
        }
        override fun current(): Pair<Long, String>? = at?.let { it to note }
    }

    private val now = ZonedDateTime.of(2026, 9, 30, 8, 0, 0, 0, ZoneId.of("Asia/Shanghai"))

    private fun args(json: String): JsonObject =
        com.xiaozhu.planner.core.llm.PlannerJson.parseToJsonElement(json) as JsonObject

    @Test
    fun `minutes 正常写入`() = runBlocking {
        val store = FakeStore()
        val out = HeartbeatTool(store) { now }.run(args("""{"minutes":60,"note":"看看进展"}"""))
        assertNotNull(store.at)
        assertEquals(now.plusMinutes(60).toInstant().toEpochMilli(), store.at)
        assertEquals("看看进展", store.note)
        assertTrue(out.contains("09:00"))
    }

    @Test
    fun `seconds 被抬到最短一分钟`() = runBlocking {
        val store = FakeStore()
        HeartbeatTool(store) { now }.run(args("""{"seconds":5}"""))
        // 5 秒 < 下界 → 抬成 1 分钟
        assertEquals(now.plusMinutes(1).toInstant().toEpochMilli(), store.at)
    }

    @Test
    fun `minutes 被夹到最长 720`() = runBlocking {
        val store = FakeStore()
        HeartbeatTool(store) { now }.run(args("""{"minutes":99999}"""))
        assertEquals(now.plusMinutes(720).toInstant().toEpochMilli(), store.at)
    }

    @Test
    fun `两个都不给时提示而不是瞎猜`() = runBlocking {
        val store = FakeStore()
        val out = HeartbeatTool(store) { now }.run(args("{}"))
        assertTrue(out.contains("seconds") && out.contains("minutes"))
        assertEquals(null, store.at)
    }
}