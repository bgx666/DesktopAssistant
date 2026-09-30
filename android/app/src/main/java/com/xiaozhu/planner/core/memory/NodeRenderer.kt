package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.data.db.NodeEntity

/**
 * 节点的文本渲染。两种用途格式不同：
 *
 * - [renderForContext]：进对话上下文的"节点消息"，必须带上画像与后续说明 ——
 *   这是小助"记得你是什么人"的唯一来源（system prompt 里不写人设）。
 * - [renderForTool]：`explore_memory_tree` 的返回，要能读、要带原文/子节点。
 */
object NodeRenderer {

    fun fmtTimeRange(start: String?, end: String?): String {
        if (start.isNullOrBlank() || end.isNullOrBlank()) return ""
        val s = start.take(16)
        val e = end.take(16)
        return if (start.take(10) == end.take(10)) "$s ~ ${end.substring(11, 16)}" else "$s ~ $e"
    }

    fun renderForContext(node: NodeEntity, details: LeafDetails, timeRange: Pair<String, String>? = null): String =
        buildString {
            append('[').append(node.id).append("] 第").append(node.roundStart).append('-')
                .append(node.roundEnd).append("条\n")
            append("[摘要] ").append(node.summary)
            val range = timeRange ?: (node.timeStart?.let { s -> node.timeEnd?.let { e -> s to e } })
            fmtTimeRange(range?.first, range?.second).takeIf { it.isNotEmpty() }?.let {
                append("\n[时间] ").append(it)
            }
            MemoryJson.decodeProfile(node.profile)?.let { p ->
                appendProfile(p) { label, items -> append("\n[").append(label).append("] ").append(items.joinToString("、")) }
            }
            details.futureNotes.takeIf { it.isNotEmpty() }?.let {
                append("\n[后续说明] ").append(it.joinToString("；"))
            }
        }

    fun renderForTool(info: NodeInfo): String = buildString {
        append("节点 ").append(info.nodeId).append("（level ").append(info.level).append("）\n")
        append("摘要：").append(info.summary)
        fmtTimeRange(info.timeRange?.first, info.timeRange?.second).takeIf { it.isNotEmpty() }?.let {
            append("\n时间：").append(it)
        }
        info.profile?.let { p -> appendProfile(p) { label, items -> append("\n").append(label).append("：").append(items.joinToString("、")) } }
        if (info.futureNotes.isNotEmpty()) append("\n后续说明：").append(info.futureNotes.joinToString("；"))
        if (info.children.isNotEmpty()) {
            append("\n\n子节点（").append(info.children.size).append(" 个，可用 explore_memory_tree 继续展开）：")
            info.children.forEach { c ->
                append("\n- ").append(c.nodeId).append("：").append(c.summary.take(120))
            }
        }
        if (info.details.isNotEmpty()) {
            append("\n\n原文（").append(info.details.size).append(" 条）：")
            info.details.forEach { m -> append("\n").append(speaker(m)).append("：").append(m.content.orEmpty()) }
        }
    }

    private inline fun appendProfile(p: ProfileInfo, emit: (String, List<String>) -> Unit) {
        if (p.preferences.isNotEmpty()) emit("喜好", p.preferences)
        if (p.personality.isNotEmpty()) emit("性格", p.personality)
        if (p.habits.isNotEmpty()) emit("习惯", p.habits)
        if (p.goals.isNotEmpty()) emit("目标", p.goals)
    }

    private fun speaker(m: ChatMessage): String = when (m.role) {
        "user" -> "用户"
        "assistant" -> "小助"
        "tool" -> "工具结果"
        else -> m.role
    }
}