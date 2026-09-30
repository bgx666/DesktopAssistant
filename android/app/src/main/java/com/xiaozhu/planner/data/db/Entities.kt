package com.xiaozhu.planner.data.db

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * 记忆树节点。字段与 PC 版 `SQLiteMemoryTree` 的 `nodes` 表对齐（结构字段留列、
 * 内容字段进 JSON），这样两边的语义与将来的导入导出都能对上。
 */
@Entity(
    tableName = "nodes",
    primaryKeys = ["characterId", "id"],
    indices = [Index("characterId", "level", "isActive"), Index("parentId")],
)
data class NodeEntity(
    val characterId: String,
    val id: String,                 // node{level}_{seq:03d}
    val level: Int,
    val summary: String,
    val parentId: String? = null,
    val roundStart: Int? = null,    // 全局消息序号区间（不是"轮"）
    val roundEnd: Int? = null,
    val timeStart: String? = null,  // yyyy-MM-dd HH:mm:ss（UTC+8）
    val timeEnd: String? = null,
    val sourceRef: String? = null,
    val details: String? = null,    // JSON: {messages:[...], future_notes:[...]}
    val profile: String? = null,    // JSON: {preferences,personality,habits,goals}
    val meta: String? = null,       // JSON（schema_version 等）
    val isActive: Boolean = true,
    /** 检索用拼接文本（摘要 + 画像 + 叶子原文）。LIKE 直接扫它，免去 FTS 对中文的分词问题。 */
    val searchText: String = "",
    val createdAt: Long = System.currentTimeMillis(),
)

/** 对话 buffer 里的一条消息。带 nodeId 的是"节点消息"（摘要渲染进上下文的那种）。 */
@Entity(tableName = "messages", indices = [Index("seq"), Index("nodeId")])
data class MessageEntity(
    @PrimaryKey(autoGenerate = true) val seq: Long = 0,
    val role: String,               // user | assistant | tool
    val content: String,
    val tsMillis: Long,
    val nodeId: String? = null,
    val nodeStart: Int? = null,     // 节点消息的起始全局序号（排序用）
    val toolCalls: String? = null,  // JSON：assistant 带工具调用时
    val toolCallId: String? = null,
    val name: String? = null,
    /**
     * 图片（JSON 数组字符串，元素是 data URL）。
     *
     * 与 PC 版一致：图片**常驻 buffer**，每轮模型调用都重发（每张最多算 384 token）。
     * 压缩成记忆节点时换成"（图片）"占位，base64 不进记忆树。
     */
    val images: String? = null,
)

/** buffer 的运行状态（重启恢复用）。 */
@Entity(tableName = "buffer_state")
data class BufferStateEntity(
    @PrimaryKey val characterId: String,
    val msgCounter: Int = 0,
    val round: Int = 0,
    val compressedTotal: Int = 0,
    val lastActivityAt: Long = 0,
    val nextHeartbeatAt: Long = 0,
    val heartbeatMinutes: Double = 0.0,
    val heartbeatNote: String = "",
    val lastPlayerMessageAt: Long = 0,
)

/**
 * 日历项（`calendar_items`）——**唯一的待办概念**（字段沿用 PC 版 `store/calendar_db.py`）。
 *
 * 日期是 `yyyy-MM-dd` 字符串、时刻是 `HH:mm` 或 NULL（NULL = 全天）。用字符串而不是
 * epoch 是为了跟 PC 版对齐（将来导入导出直接对得上）；同格式下字典序即时间序。
 */
@Entity(
    tableName = "calendar_items",
    indices = [Index("date", "time"), Index("seriesId"), Index("status", "remindedAt")],
)
data class CalendarItemEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val date: String,                       // yyyy-MM-dd（UTC+8 本地日）
    val time: String? = null,               // HH:mm；NULL = 全天
    val kind: String = KIND_EVENT,          // event 日程 / task 当天任务
    val content: String,
    val detail: String = "",
    val status: String = STATUS_TODO,       // todo / done / cancelled
    val remindedAt: String? = null,         // 到点提醒过的时刻（防重复提醒）
    val createdAt: Long = System.currentTimeMillis(),
    val doneAt: Long? = null,
    val seriesId: String? = null,           // 重复系列标识（NULL = 一次性）
    val repeat: String? = null,             // daily / weekly / weekdays
    val repeatUntil: String? = null,        // 该系列已生成到的最后日期（滚动续期用）
) {
    companion object {
        const val KIND_EVENT = "event"
        const val KIND_TASK = "task"
        const val STATUS_TODO = "todo"
        const val STATUS_DONE = "done"
        const val STATUS_CANCELLED = "cancelled"
        val KINDS = listOf(KIND_EVENT, KIND_TASK)
        val STATUSES = listOf(STATUS_TODO, STATUS_DONE, STATUS_CANCELLED)
        val REPEAT_RULES = listOf("daily", "weekly", "weekdays")
    }
}