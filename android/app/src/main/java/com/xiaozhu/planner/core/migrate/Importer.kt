package com.xiaozhu.planner.core.migrate

import androidx.room.withTransaction
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.data.db.PlannerDatabase

/**
 * 把导出的数据写进本机库。
 *
 * 两条硬规矩：
 *
 * 1. **要么全成、要么全不成**（一个事务）。迁移是一次性动作，
 *    写进去一半（比如节点进了、原文没进）之后，用户看到的是一个
 *    "记得有这回事但说不出细节"的记忆树——比没有更难查。
 * 2. **不覆盖正在用的数据，除非用户明确同意**。[preview] 先报本地已有什么，
 *    UI 拿到确认后才传 `wipeFirst = true`。
 */
class Importer(private val db: PlannerDatabase) {

    data class Preview(
        val nodes: Int,
        val leavesWithDetails: Int,
        val bufferMessages: Int,
        val calendarItems: Int,
        val warnings: List<String>,
        val localNodes: Int,
        val localMessages: Int,
        val localCalendar: Int,
        val exportedAt: String?,
        val source: String?,
    ) {
        val hasLocalData: Boolean get() = localNodes > 0 || localMessages > 0 || localCalendar > 0
    }

    data class Result(
        val nodes: Int,
        val messages: Int,
        val calendar: Int,
        val warnings: List<String>,
    )

    suspend fun preview(payload: ExportPayload): Preview = Preview(
        nodes = payload.nodes.size,
        leavesWithDetails = payload.nodes.count { it.level == 0 && it.details != null },
        bufferMessages = (payload.buffer?.recentBuffer as? kotlinx.serialization.json.JsonArray)?.size ?: 0,
        calendarItems = payload.calendarItems.size,
        warnings = payload.warnings,
        localNodes = db.nodes().count(MemoryStore.DEFAULT_CHARACTER),
        localMessages = db.messages().rawCount(),
        localCalendar = db.calendar().rangeAll("0000-01-01", "9999-12-31").size,
        exportedAt = payload.exportedAt,
        source = (payload.source?.get("memory_tree") as? kotlinx.serialization.json.JsonPrimitive)?.content,
    )

    suspend fun apply(
        payload: ExportPayload,
        wipeFirst: Boolean,
        characterId: String = MemoryStore.DEFAULT_CHARACTER,
    ): Result {
        val mapped = PcImport.map(payload, characterId)

        db.withTransaction {
            if (wipeFirst) {
                db.messages().clear()
                db.nodes().clearAll(characterId)
                db.calendar().clear()
            }
            if (mapped.nodes.isNotEmpty()) db.nodes().insertAllReplace(mapped.nodes)
            if (mapped.messages.isNotEmpty()) db.messages().insertAll(mapped.messages)
            if (mapped.calendar.isNotEmpty()) db.calendar().insertAll(mapped.calendar)
            mapped.bufferState?.let { db.buffer().save(it) }
        }

        return Result(
            nodes = mapped.nodes.size,
            messages = mapped.messages.size,
            calendar = mapped.calendar.size,
            warnings = mapped.skipped,
        )
    }
}
