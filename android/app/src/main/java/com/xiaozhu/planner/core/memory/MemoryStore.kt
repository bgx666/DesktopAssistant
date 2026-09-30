package com.xiaozhu.planner.core.memory

import androidx.room.withTransaction
import androidx.sqlite.db.SimpleSQLiteQuery
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.data.db.BufferStateEntity
import com.xiaozhu.planner.data.db.MessageEntity
import com.xiaozhu.planner.data.db.NodeEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.serialization.json.JsonObject

/**
 * 分层摘要记忆树的存储层（复刻 PC 版 `SQLiteMemoryTree` 的语义）：
 *
 * - 叶子节点 `node{level}_{seq:03d}`，序号从库里 MAX 恢复 —— 重启不重号；
 * - `roundStart/roundEnd` 是**全局消息序号区间**（不是"轮"，沿用 PC 版的命名）；
 * - 父节点时间范围 = 子节点 min/max；子节点挂上 parentId 并 `isActive = 0`；
 * - 只有"尚未被合并"的节点是 active，也就是会出现在上下文里的那些。
 */
class MemoryStore(
    private val db: PlannerDatabase,
    private val characterId: String = DEFAULT_CHARACTER,
) {

    // ── 节点写入 ─────────────────────────────────────────────

    suspend fun addLeaf(
        summary: String,
        roundRange: Pair<Int, Int>,
        sourceRef: String? = null,
        details: LeafDetails = LeafDetails(),
        profile: ProfileInfo? = null,
        meta: JsonObject = JsonObject(emptyMap()),
        timeRange: Pair<String, String>? = null,
    ): String = db.withTransaction {
        val id = nextId(0)
        db.nodes().insert(
            NodeEntity(
                characterId = characterId,
                id = id,
                level = 0,
                summary = summary.trim(),
                parentId = null,
                roundStart = roundRange.first,
                roundEnd = roundRange.second,
                timeStart = timeRange?.first,
                timeEnd = timeRange?.second,
                sourceRef = sourceRef,
                details = MemoryJson.encodeDetails(details),
                profile = profile?.takeIf { !it.isEmpty }?.let { MemoryJson.encodeProfile(it) },
                meta = meta.toString(),
                isActive = true,
                searchText = MemorySearch.searchableText(summary, profile, details.messages),
            )
        )
        id
    }

    /** 把若干同层子节点合并成父节点，返回父节点 id。 */
    suspend fun compact(childIds: List<String>, out: MemoryNodeOutput): String {
        require(childIds.isNotEmpty()) { "compact: childIds 不能为空" }
        return db.withTransaction {
            val children = childIds.map { cid ->
                db.nodes().byId(characterId, cid) ?: error("compact: 子节点不存在 $cid")
            }
            val level = children.maxOf { it.level } + 1
            val id = nextId(level)
            db.nodes().insert(
                NodeEntity(
                    characterId = characterId,
                    id = id,
                    level = level,
                    summary = out.summary.trim(),
                    parentId = null,
                    roundStart = children.mapNotNull { it.roundStart }.minOrNull(),
                    roundEnd = children.mapNotNull { it.roundEnd }.maxOrNull(),
                    timeStart = children.mapNotNull { it.timeStart }.minOrNull(),
                    timeEnd = children.mapNotNull { it.timeEnd }.maxOrNull(),
                    details = null,
                    profile = out.profile.takeIf { !it.isEmpty }?.let { MemoryJson.encodeProfile(it) },
                    meta = out.meta.toString(),
                    isActive = true,
                    searchText = MemorySearch.searchableText(out.summary, out.profile, emptyList()),
                )
            )
            db.nodes().attachToParent(characterId, id, childIds)
            id
        }
    }

    /** 该层下一个可用 id。序号取库里 MAX，所以重启后不会重号。 */
    private suspend fun nextId(level: Int): String {
        val seq = (db.nodes().maxSeqAtLevel(characterId, level) ?: 0) + 1
        return "node${level}_%03d".format(seq)
    }

    // ── 节点读取 ─────────────────────────────────────────────

    suspend fun levelCount(level: Int): Int = db.nodes().activeCountAtLevel(characterId, level)

    suspend fun activeAtLevel(level: Int): List<NodeEntity> = db.nodes().activeAtLevel(characterId, level)

    /** 根 = 层级最高的节点（回忆从它开始往下钻）。 */
    suspend fun rootId(): String? = db.nodes().topNodeId(characterId)

    suspend fun nodeById(id: String): NodeEntity? = db.nodes().byId(characterId, id)

    suspend fun nodeCount(): Int = db.nodes().count(characterId)

    /**
     * 节点有效时间范围：自身优先；为空（历史/导入数据）时递归聚合子节点。
     */
    suspend fun effectiveTimeRange(nodeId: String): Pair<String, String>? {
        val node = db.nodes().byId(characterId, nodeId) ?: return null
        val own = node.timeStart to node.timeEnd
        if (own.first != null && own.second != null) return own.first!! to own.second!!
        var start = own.first
        var end = own.second
        db.nodes().childrenOf(characterId, nodeId).forEach { child ->
            effectiveTimeRange(child.id)?.let { (cs, ce) ->
                if (start == null || cs < start!!) start = cs
                if (end == null || ce > end!!) end = ce
            }
        }
        return if (start != null && end != null) start!! to end!! else null
    }

    /** 展开一个节点：叶子给原文，父节点给子节点摘要。 */
    suspend fun nodeInfo(nodeId: String): NodeInfo? {
        val node = db.nodes().byId(characterId, nodeId) ?: return null
        val profile = MemoryJson.decodeProfile(node.profile)
        val range = effectiveTimeRange(nodeId)
        return if (node.level == 0) {
            val details = MemoryJson.decodeDetails(node.details)
            NodeInfo(
                nodeId = node.id,
                level = node.level,
                summary = node.summary,
                timeRange = range,
                profile = profile,
                futureNotes = details.futureNotes,
                details = details.messages,
            )
        } else {
            NodeInfo(
                nodeId = node.id,
                level = node.level,
                summary = node.summary,
                timeRange = range,
                profile = profile,
                children = db.nodes().childrenOf(characterId, nodeId).map { child ->
                    NodeInfo(
                        nodeId = child.id,
                        level = child.level,
                        summary = child.summary,
                        timeRange = null,
                        profile = MemoryJson.decodeProfile(child.profile),
                    )
                },
            )
        }
    }

    /** 内容检索（LIKE，见 [MemorySearch] 里对"为什么不用 FTS"的说明）。 */
    suspend fun search(query: String, limit: Int = 5): List<MemoryHit> {
        val terms = MemorySearch.terms(query)
        if (terms.isEmpty()) return emptyList()
        val (sql, args) = MemorySearch.buildSql(characterId, terms, limit)
        return db.nodes().search(SimpleSQLiteQuery(sql, args.toTypedArray())).map { node ->
            MemoryHit(
                nodeId = node.id,
                level = node.level,
                roundStart = node.roundStart,
                timeStart = node.timeStart,
                snippet = MemorySearch.snippet(
                    node.searchText.ifBlank { node.summary }, terms
                ),
            )
        }
    }

    // ── buffer ──────────────────────────────────────────────

    /**
     * 找以前发过的图片（`list_images` 工具用）。
     *
     * 检索两条线，合并后按时间倒序：
     * 1. **buffer 里还留着的图**（`messages.images` 非空）
     * 2. **已经压进记忆树的图**（节点的 `details.image_refs`）
     *
     * 第二条是关键——用户问的往往是"很久以前"的图，那时原消息早被压缩掉了，
     * 只有节点里那份引用还在。
     */
    suspend fun findImages(
        fromMillis: Long?,
        toMillis: Long?,
        query: String,
        limit: Int,
    ): List<ImageHit> {
        val hits = mutableListOf<ImageHit>()

        // ① buffer 里的
        val from = fromMillis ?: 0L
        val to = toMillis ?: Long.MAX_VALUE
        db.messages().withImagesBetween(from, to, limit * 2).forEach { row ->
            val said = if (row.role == "user") row.content else ""
            if (query.isNotBlank() && !said.contains(query, ignoreCase = true)) return@forEach
            MessageMapping.rawImagesOf(row).forEachIndexed { index, ref ->
                hits += ImageHit(
                    id = "msg:${row.seq}:$index",
                    ts = tsText(row.tsMillis),
                    said = said,
                    nodeSummary = "",
                    ref = ref,
                )
            }
        }

        // ② 记忆树节点里的
        db.nodes().allForExport(characterId).forEach { node ->
            val details = MemoryJson.decodeDetails(node.details) ?: return@forEach
            if (details.imageRefs.isEmpty()) return@forEach
            details.imageRefs.forEachIndexed { index, ref ->
                val inRange = inRange(ref.ts, from, to)
                if (!inRange) return@forEachIndexed
                if (query.isNotBlank() &&
                    !ref.said.contains(query, ignoreCase = true) &&
                    !node.summary.contains(query, ignoreCase = true)
                ) return@forEachIndexed
                hits += ImageHit(
                    id = "node:${node.id}:$index",
                    ts = ref.ts,
                    said = ref.said,
                    nodeSummary = node.summary,
                    ref = ref.ref,
                )
            }
        }

        return hits.sortedByDescending { it.ts }.take(limit)
    }

    /** 时间范围过滤（节点里存的是 `yyyy-MM-dd HH:mm:ss` 文本）。 */
    private fun inRange(ts: String, from: Long, to: Long): Boolean {
        if (from <= 0L && to == Long.MAX_VALUE) return true
        val millis = runCatching {
            java.time.LocalDateTime.parse(ts.replace(' ', 'T'))
                .atZone(java.time.ZoneId.of("Asia/Shanghai")).toInstant().toEpochMilli()
        }.getOrNull() ?: return true
        return millis in from..to
    }

    private fun tsText(millis: Long): String =
        java.time.Instant.ofEpochMilli(millis).atZone(java.time.ZoneId.of("Asia/Shanghai"))
            .format(java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))


    suspend fun appendMessage(
        role: String,
        content: String,
        tsMillis: Long = System.currentTimeMillis(),
        nodeId: String? = null,
        nodeStart: Int? = null,
        toolCalls: String? = null,
        toolCallId: String? = null,
        name: String? = null,
    ): Long = db.messages().insert(
        MessageEntity(
            role = role,
            content = content,
            tsMillis = tsMillis,
            nodeId = nodeId,
            nodeStart = nodeStart,
            toolCalls = toolCalls,
            toolCallId = toolCallId,
            name = name,
        )
    )

    suspend fun rawMessages(): List<MessageEntity> = db.messages().raw()

    suspend fun rawCount(): Int = db.messages().rawCount()

    suspend fun firstRaw(n: Int): List<MessageEntity> = db.messages().firstRaw(n)

    suspend fun nodeMessages(): List<MessageEntity> = db.messages().nodeMessages()

    /** 这些节点在 buffer 里的消息（合并时要把子节点的消息换成父节点的）。 */
    suspend fun messagesByNodeIds(nodeIds: List<String>): List<MessageEntity> =
        if (nodeIds.isEmpty()) emptyList() else db.messages().byNodeIds(nodeIds)

    suspend fun deleteMessages(seqs: List<Long>) {
        if (seqs.isNotEmpty()) db.messages().deleteBySeq(seqs)
    }

    suspend fun saveBufferState(state: BufferStateEntity) = db.buffer().save(state)

    suspend fun loadBufferState(): BufferStateEntity? = db.buffer().load(characterId)

    companion object {
        const val DEFAULT_CHARACTER = "assistant"
    }
}