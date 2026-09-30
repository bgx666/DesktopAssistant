package com.xiaozhu.planner.core.migrate

import androidx.room.withTransaction
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

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

    /**
     * 把当前库导出成同一个 JSON 格式（备份 / 换机 / 人工查看）。
     *
     * 与 PC 的导出脚本产出**同一套结构**，所以两边可以互相导：
     * 手机上备份的文件，PC 或新手机都能直接读。
     */
    suspend fun export(): ExportPayload {
        val nodes = db.nodes().allForExport(MemoryStore.DEFAULT_CHARACTER)
        val messages = db.messages().all()
        val calendar = db.calendar().rangeAll("0000-01-01", "9999-12-31")
        val state = db.buffer().load(MemoryStore.DEFAULT_CHARACTER)

        return ExportPayload(
            format = ExportPayload.EXPECTED_FORMAT,
            formatVersion = ExportPayload.SUPPORTED_VERSION,
            exportedAt = java.time.ZonedDateTime.now(java.time.ZoneId.of("Asia/Shanghai"))
                .format(java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME),
            source = kotlinx.serialization.json.buildJsonObject {
                put("memory_tree", "android:planner.db")
                put("calendar", "android:planner.db")
            },
            counts = ExportCounts(
                nodes = nodes.size,
                nodesWithDetails = nodes.count { it.level == 0 && it.details != null },
                bufferPresent = messages.isNotEmpty(),
                calendarItems = calendar.size,
            ),
            nodes = nodes.map { it.toExportNode() },
            buffer = ExportBuffer(
                characterId = MemoryStore.DEFAULT_CHARACTER,
                recentBuffer = messages.toExportBuffer(),
                msgCounter = state?.msgCounter ?: 0,
                round = state?.round ?: 0,
                compressedTotal = state?.compressedTotal ?: 0,
                lastActivityAt = state?.lastActivityAt?.let { it / 1000.0 },
                nextHeartbeatAt = state?.nextHeartbeatAt?.let { it / 1000.0 },
                heartbeatMinutes = state?.heartbeatMinutes,
                heartbeatNote = state?.heartbeatNote,
                lastPlayerMessageAt = state?.lastPlayerMessageAt?.let { it / 1000.0 },
            ),
            calendarItems = calendar.map { it.toExportItem() },
            warnings = emptyList(),
        )
    }

    private fun com.xiaozhu.planner.data.db.NodeEntity.toExportNode() = ExportNode(
        characterId = characterId,
        id = id,
        level = level,
        summary = summary,
        parentId = parentId,
        roundStart = roundStart,
        roundEnd = roundEnd,
        timeStart = timeStart,
        timeEnd = timeEnd,
        sourceRef = sourceRef,
        details = details?.let { kotlinx.serialization.json.Json.parseToJsonElement(it) },
        profile = profile?.let { kotlinx.serialization.json.Json.parseToJsonElement(it) },
        meta = meta?.let { kotlinx.serialization.json.Json.parseToJsonElement(it) },
        isActive = if (isActive) 1 else 0,
        createdAt = createdAt / 1000,
    )

    private fun com.xiaozhu.planner.data.db.CalendarItemEntity.toExportItem() = ExportCalendarItem(
        id = id,
        date = date,
        time = time,
        kind = kind,
        content = content,
        detail = detail,
        status = status,
        remindedAt = remindedAt,
        createdAt = null,
        doneAt = doneAt?.let {
            java.time.Instant.ofEpochMilli(it).atZone(java.time.ZoneId.of("Asia/Shanghai"))
                .format(java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))
        },
        seriesId = seriesId,
        repeat = repeat,
        repeatUntil = repeatUntil,
    )

    /** 消息还原成 PC 那套 `{type, data}` 形状（含 metadata.ts / node_id）。 */
    private fun List<com.xiaozhu.planner.data.db.MessageEntity>.toExportBuffer():
        kotlinx.serialization.json.JsonElement {
        val arr = kotlinx.serialization.json.JsonArray(
            map { m ->
                val type = when (m.role) {
                    "user" -> "human"
                    "assistant" -> "ai"
                    else -> m.role
                }
                kotlinx.serialization.json.buildJsonObject {
                    put("type", type)
                    putJsonObject("data") {
                        put("content", m.content)
                        put("type", type)
                        val meta = kotlinx.serialization.json.buildJsonObject {
                            // 原始时间戳要带上：没有它，对方新建的节点时间范围会全错
                            put("ts", java.time.Instant.ofEpochMilli(m.tsMillis)
                                .atZone(java.time.ZoneId.of("Asia/Shanghai"))
                                .format(java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")))
                            m.nodeId?.let { put("node_id", it) }
                            m.nodeStart?.let { put("node_start", it) }
                        }
                        put("metadata", meta)
                    }
                }
            }
        )
        return arr
    }
}
