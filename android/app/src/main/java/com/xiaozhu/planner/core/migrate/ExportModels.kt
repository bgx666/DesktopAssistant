package com.xiaozhu.planner.core.migrate

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * PC 版导出文件的结构（`scripts/export_to_android.py` 的产物）。
 *
 * 所有字段都给默认值：导出脚本将来加字段时，旧版 App 读到新文件也不该崩——
 * 迁移是一次性动作，**读不动就报清楚**，不要写进一半数据。
 */
@Serializable
data class ExportPayload(
    val format: String = "",
    @SerialName("format_version") val formatVersion: Int = 0,
    @SerialName("exported_at") val exportedAt: String? = null,
    val source: JsonObject? = null,
    val counts: ExportCounts? = null,
    val nodes: List<ExportNode> = emptyList(),
    val buffer: ExportBuffer? = null,
    @SerialName("calendar_items") val calendarItems: List<ExportCalendarItem> = emptyList(),
    val warnings: List<String> = emptyList(),
) {
    companion object {
        const val EXPECTED_FORMAT = "planner-android-export"
        const val SUPPORTED_VERSION = 1
    }
}

@Serializable
data class ExportCounts(
    val nodes: Int = 0,
    @SerialName("nodes_with_details") val nodesWithDetails: Int = 0,
    @SerialName("buffer_present") val bufferPresent: Boolean = false,
    @SerialName("calendar_items") val calendarItems: Int = 0,
)

/** PC `memory_tree.db` 的 `nodes` 一行（details/profile/meta 已还原成对象）。 */
@Serializable
data class ExportNode(
    @SerialName("character_id") val characterId: String = "assistant",
    val id: String,
    val level: Int = 0,
    val summary: String = "",
    @SerialName("parent_id") val parentId: String? = null,
    @SerialName("round_start") val roundStart: Int? = null,
    @SerialName("round_end") val roundEnd: Int? = null,
    @SerialName("time_start") val timeStart: String? = null,
    @SerialName("time_end") val timeEnd: String? = null,
    @SerialName("source_ref") val sourceRef: String? = null,
    val details: JsonElement? = null,
    val profile: JsonElement? = null,
    val meta: JsonElement? = null,
    @SerialName("is_active") val isActive: Int = 1,
    @SerialName("created_at") val createdAt: Long? = null,
)

/** PC `buffer_state` 那一行（时间字段是 epoch **秒**，带小数）。 */
@Serializable
data class ExportBuffer(
    @SerialName("character_id") val characterId: String = "assistant",
    @SerialName("recent_buffer") val recentBuffer: JsonElement? = null,
    @SerialName("msg_counter") val msgCounter: Int = 0,
    val round: Int = 0,
    @SerialName("compressed_total") val compressedTotal: Int = 0,
    @SerialName("last_activity_at") val lastActivityAt: Double? = null,
    @SerialName("next_heartbeat_at") val nextHeartbeatAt: Double? = null,
    @SerialName("heartbeat_minutes") val heartbeatMinutes: Double? = null,
    @SerialName("heartbeat_note") val heartbeatNote: String? = null,
    @SerialName("last_player_message_at") val lastPlayerMessageAt: Double? = null,
    @SerialName("reminded_overdue") val remindedOverdue: JsonElement? = null,
)

/** PC `calendar_items` 一行（`created_at`/`done_at` 是文本日期，不是 epoch）。 */
@Serializable
data class ExportCalendarItem(
    val id: Long = 0,
    val date: String,
    val time: String? = null,
    val kind: String = "event",
    val content: String = "",
    val detail: String = "",
    val status: String = "todo",
    @SerialName("reminded_at") val remindedAt: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("done_at") val doneAt: String? = null,
    @SerialName("series_id") val seriesId: String? = null,
    val repeat: String? = null,
    @SerialName("repeat_until") val repeatUntil: String? = null,
)
