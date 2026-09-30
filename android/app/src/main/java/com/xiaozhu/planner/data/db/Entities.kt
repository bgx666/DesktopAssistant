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