package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.LlmCompleter
import com.xiaozhu.planner.data.db.BufferStateEntity
import com.xiaozhu.planner.data.db.MessageEntity
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** 压缩参数（默认值与 PC 版一致；设置页可改，改完立即生效）。 */
data class CompressionSettings(
    val trigger: Int = 60,
    val keep: Int = 20,
    val compactThreshold: Int = 8,
    val compactFactor: Int = 4,
) {
    /**
     * 参数不合法就返回原因（PC 版同款判据）。
     *
     * 不合法时**不拦保存**——PC 就是在示意图上画一行红字，参数照存；
     * 这里保持同样的宽容度，只是把话说清楚。
     */
    fun problem(): String? = when {
        keep >= trigger -> "保留条数（$keep）得小于触发条数（$trigger），否则永远压不动。"
        compactFactor >= compactThreshold ->
            "合并个数（$compactFactor）得小于合并阈值（$compactThreshold），否则每层一合并就立刻再合并。"
        else -> null
    }
}

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
        // 这一段里出现过的图片，**要一起交给摘要模型**（见 callCompress 的注释）：
        // 不交的话，压缩后记忆里只剩一个"（图片）"，谁也不知道图里是什么。
        val images = batch.flatMap { row ->
            MessageMapping.imagesOf(row)
        }.take(MAX_SUMMARY_IMAGES)
        val out = callCompress(futureText, CompressionPrompts.leaf(original), images)
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
            // 但**引用要留下**：以后用户问"那天的天空什么样"，模型靠它把图找回来
            // （list_images / show_image 两个工具）。原图在文件里，不在节点里。
            imageRefs = batch.flatMap { row ->
                val refs = MessageMapping.rawImagesOf(row)
                if (refs.isEmpty()) emptyList()
                else refs.map { ref ->
                    ImageRef(
                        ref = ref,
                        ts = ts(row.tsMillis),
                        said = if (row.role == "user") row.content.take(120) else "",
                    )
                }
            },
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

    /**
     * 压缩请求。
     *
     * [images] 是这一段对话里出现过的图片：**必须一起发给摘要模型**。
     * 图片本体不会进记忆树（base64 写进 details 会让节点膨胀几百倍），
     * 但"图里是什么"要由模型写进摘要——否则压缩之后记忆里只剩一个"（图片）"占位，
     * 之后再也答不出"你上次发的那张图里写了什么"。
     *
     * 图片数量封顶 [MAX_SUMMARY_IMAGES]：压缩是后台任务，一次塞十几张图又慢又贵。
     */
    private suspend fun callCompress(
        futureText: String,
        instruction: String,
        images: List<String> = emptyList(),
    ): MemoryNodeOutput? {
        val messages = buildList {
            add(ChatMessage.system(systemPrompt))
            if (futureText.isNotBlank()) {
                add(
                    ChatMessage.user(
                        "（以下是这段内容**之后**的消息，只用来判断后续有没有澄清或修正，不是要压缩的内容）\n$futureText"
                    )
                )
            }
            add(
                if (images.isEmpty()) {
                    ChatMessage.user(instruction)
                } else {
                    ChatMessage(
                        role = "user",
                        content = instruction + "\n\n（上面那段对话里带过的图片附在下面，" +
                            "请在摘要里写清图片内容——之后图片本身就不会留在上下文里了）",
                        images = images,
                    )
                }
            )
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

    private companion object {
        /** 一次压缩最多把几张图交给摘要模型（后台任务，多了又慢又贵）。 */
        const val MAX_SUMMARY_IMAGES = 4
    }

    private fun ts(millis: Long): String =
        Instant.ofEpochMilli(millis).atZone(zone).format(tsFormat)
}

