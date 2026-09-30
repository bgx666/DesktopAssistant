package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.LlmCompleter
import com.xiaozhu.planner.data.db.BufferStateEntity
import com.xiaozhu.planner.data.db.MessageEntity
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** 压缩参数（与 PC 版默认值一致，可后续放进设置页）。 */
data class CompressionSettings(
    val trigger: Int = 60,
    val keep: Int = 20,
    val compactThreshold: Int = 8,
    val compactFactor: Int = 4,
)

sealed interface CompressionOutcome {
    data object Skipped : CompressionOutcome
    data class LeafCreated(val nodeId: String, val messageCount: Int) : CompressionOutcome
    data class ParentCreated(val nodeId: String, val level: Int, val childCount: Int) : CompressionOutcome
    data class Failed(val reason: String) : CompressionOutcome
}

/**
 * 分层摘要压缩（复刻 PC 版 `compress_snapshot` 的流程）：
 *
 * 1. 原始消息 ≥ trigger → 把**最早的** `raw - keep` 条压成一个叶子节点；
 * 2. 某层活跃节点 ≥ compactThreshold → 取最早的 compactFactor 个并成父节点，逐层向上；
 * 3. buffer 同步：删掉被压缩的消息、换上渲染好的节点消息；`compressedTotal` 前进。
 *
 * 两条纪律：
 * - **摘要为空就整体放弃**，绝不用空摘要覆盖掉真实对话；
 * - **压缩失败不动 buffer**（宁可下次再压，也不能把上下文弄坏）。
 */
class Compressor(
    private val store: MemoryStore,
    private val llm: LlmCompleter,
    private val systemPrompt: String,
    private val settings: CompressionSettings = CompressionSettings(),
) {

    private val tsFormat = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")
    private val zone = ZoneId.of("Asia/Shanghai")

    suspend fun compressIfNeeded(): List<CompressionOutcome> {
        val outcomes = mutableListOf<CompressionOutcome>()

        val raw = store.rawMessages()
        if (raw.size >= settings.trigger) {
            val batchSize = raw.size - settings.keep
            if (batchSize > 0) {
                outcomes += summarizeLeaf(
                    batch = raw.take(batchSize),
                    future = raw.drop(batchSize),
                )
            }
        }

        var level = 0
        while (store.levelCount(level) >= settings.compactThreshold) {
            val outcome = compactLevel(level) ?: break
            outcomes += outcome
            level += 1
        }

        if (outcomes.isEmpty()) outcomes += CompressionOutcome.Skipped
        return outcomes
    }

    // ── 叶子压缩 ────────────────────────────────────────────

    private suspend fun summarizeLeaf(
        batch: List<MessageEntity>,
        future: List<MessageEntity>,
    ): CompressionOutcome {
        if (batch.isEmpty()) return CompressionOutcome.Skipped

        val original = batch.joinToString("\n") { "${speaker(it)}: ${it.content}" }
        val futureText = future.joinToString("\n") { "${speaker(it)}: ${it.content}" }
        val out = callCompress(futureText, CompressionPrompts.leaf(original))
            ?: return CompressionOutcome.Failed("压缩调用失败或输出无法解析")
        if (out.summary.isBlank()) return CompressionOutcome.Failed("摘要为空，放弃本次压缩")

        val state = store.loadBufferState() ?: BufferStateEntity(characterId = MemoryStore.DEFAULT_CHARACTER)
        val start = state.compressedTotal
        val end = start + batch.size - 1
        val timeRange = ts(batch.first().tsMillis) to ts(batch.last().tsMillis)

        val details = LeafDetails(
            // 图片换成"（图片）"占位（PC 版同款）：base64 绝不进记忆树——
            // 那是几 MB 的字符串，写进 details 会让节点膨胀几百倍，而且模型也读不到"图"本身
            messages = batch.map { row ->
                val msg = MessageMapping.toChatMessage(row)
                if (msg.images.isEmpty()) msg
                else msg.copy(
                    content = listOfNotNull(msg.content?.takeIf { it.isNotBlank() }, "（图片）")
                        .joinToString(" "),
                    images = emptyList(),
                )
            },
            futureNotes = out.futureNotes,
        )
        val nodeId = store.addLeaf(
            summary = out.summary,
            roundRange = start to end,
            details = details,
            profile = out.profile.takeIf { !it.isEmpty },
            meta = out.meta,
            timeRange = timeRange,
        )

        // buffer：删原文、插节点消息
        store.deleteMessages(batch.map { it.seq })
        val node = store.nodeById(nodeId)!!
        store.appendMessage(
            role = "assistant",
            content = NodeRenderer.renderForContext(node, details, timeRange),
            tsMillis = batch.last().tsMillis,
            nodeId = nodeId,
            nodeStart = start,
        )
        store.saveBufferState(state.copy(compressedTotal = end + 1))
        return CompressionOutcome.LeafCreated(nodeId, batch.size)
    }

    // ── 向上合并 ────────────────────────────────────────────

    private suspend fun compactLevel(level: Int): CompressionOutcome? {
        val nodes = store.activeAtLevel(level)
        if (nodes.size < settings.compactThreshold) return null

        val picked = nodes.take(settings.compactFactor)
        val childText = picked.joinToString("\n\n") { node ->
            NodeRenderer.renderForCompression(node, MemoryJson.decodeDetails(node.details))
        }
        val out = callCompress("", CompressionPrompts.parent(picked.size, childText)) ?: return null
        if (out.summary.isBlank()) return null

        val childIds = picked.map { it.id }
        val parentId = store.compact(childIds, out)

        // buffer：子节点消息整体换成父节点消息
        val childMsgs = store.messagesByNodeIds(childIds)
        if (childMsgs.isNotEmpty()) {
            store.deleteMessages(childMsgs.map { it.seq })
            val nodeStart = childMsgs.mapNotNull { it.nodeStart }.minOrNull() ?: picked.first().roundStart ?: 0
            val parent = store.nodeById(parentId)!!
            store.appendMessage(
                role = "assistant",
                content = NodeRenderer.renderForContext(parent, LeafDetails(), store.effectiveTimeRange(parentId)),
                tsMillis = childMsgs.maxOf { it.tsMillis },
                nodeId = parentId,
                nodeStart = nodeStart,
            )
        }
        return CompressionOutcome.ParentCreated(parentId, level + 1, picked.size)
    }

    // ── 压缩请求 ────────────────────────────────────────────

    private suspend fun callCompress(futureText: String, instruction: String): MemoryNodeOutput? {
        val messages = buildList {
            add(ChatMessage.system(systemPrompt))
            if (futureText.isNotBlank()) {
                add(
                    ChatMessage.user(
                        "（以下是这段内容**之后**的消息，只用来判断后续有没有澄清或修正，不是要压缩的内容）\n$futureText"
                    )
                )
            }
            add(ChatMessage.user(instruction))
        }
        val raw = runCatching { llm.complete(messages) }.getOrNull() ?: return null
        return NodeOutputParser.parse(raw)
    }

    private fun speaker(m: MessageEntity): String = when (m.role) {
        "user" -> "用户"
        "assistant" -> "小助"
        "tool" -> "工具结果"
        else -> m.role
    }

    private fun ts(millis: Long): String =
        Instant.ofEpochMilli(millis).atZone(zone).format(tsFormat)
}

