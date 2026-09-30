package com.xiaozhu.planner.core.migrate

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.core.llm.ToolCallFunction
import com.xiaozhu.planner.core.memory.LeafDetails
import com.xiaozhu.planner.core.memory.MemoryJson
import com.xiaozhu.planner.core.memory.MemorySearch
import com.xiaozhu.planner.core.memory.ProfileInfo
import com.xiaozhu.planner.data.db.BufferStateEntity
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.MessageEntity
import com.xiaozhu.planner.data.db.NodeEntity
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * PC 导出 → 安卓表的**纯映射**（不碰 Room，所以能直接单测）。
 *
 * 三个必须处理的差异：
 *
 * 1. **角色名**：PC 用 LangChain 的 `human`/`ai`/`tool`，安卓用 OpenAI 的
 *    `user`/`assistant`/`tool`。名字不转过去，导入后模型会把用户的话当成
 *    别人的旁白。
 * 2. **工具调用的形状**：LangChain 是 `{name, args: {...}, id}`，
 *    OpenAI 是 `{id, type, function:{name, arguments: "<json 字符串>"}}`。
 *    `arguments` 必须是**字符串**，给成对象下一轮请求直接 400。
 * 3. **时间单位**：PC 的 `buffer_state` 时间戳是 epoch **秒**（带小数），
 *    安卓用毫秒；而 `calendar_items.created_at` 在 PC 是**文本日期**。
 *    这两处单位搞混，表现是"心跳时间在 1970 年"或"日程排到 5 万年后"。
 */
object PcImport {

    private val PC_TIME = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")

    /** 导入结果（给 UI 报数用）。 */
    data class Mapped(
        val nodes: List<NodeEntity>,
        val messages: List<MessageEntity>,
        val calendar: List<CalendarItemEntity>,
        val bufferState: BufferStateEntity?,
        val skipped: List<String>,
    )

    fun map(payload: ExportPayload, characterId: String): Mapped {
        val skipped = mutableListOf<String>()
        val nodes = payload.nodes.map { it.toEntity(characterId, skipped) }
        val messages = payload.buffer?.recentBuffer.toMessages(skipped)
        val calendar = payload.calendarItems.mapNotNull { it.toEntity(skipped) }
        val state = payload.buffer?.toState(characterId, payload.nodes)
        return Mapped(nodes, messages, calendar, state, skipped)
    }

    // ── 记忆节点 ────────────────────────────────────────────

    private fun ExportNode.toEntity(characterId: String, skipped: MutableList<String>): NodeEntity {
        val profile = profile.toProfile()
        val details = details.toLeafDetails()
        return NodeEntity(
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
            details = details?.let { MemoryJson.encodeDetails(it) },
            profile = profile?.takeIf { !it.isEmpty }?.let { MemoryJson.encodeProfile(it) },
            meta = meta?.takeIf { it !is JsonNull }?.toString(),
            isActive = isActive != 0,
            // 检索文本要重建：PC 库里没有这一列（它是安卓为 LIKE 检索加的）
            searchText = MemorySearch.searchableText(summary, profile, details?.messages.orEmpty()),
            createdAt = createdAt?.let { it * 1000 } ?: System.currentTimeMillis(),
        ).also {
            if (level == 0 && summary.isNotBlank() && details?.messages.isNullOrEmpty()) {
                skipped += "节点 $id 没有原文（导出时用了 --no-details，或这段确实没留下原文）"
            }
        }
    }

    private fun JsonElement?.toProfile(): ProfileInfo? {
        val obj = this as? JsonObject ?: return null
        fun list(key: String): List<String> =
            (obj[key] as? JsonArray)?.mapNotNull { it.jsonPrimitive.contentOrNull }.orEmpty()
        return ProfileInfo(
            preferences = list("preferences"),
            personality = list("personality"),
            habits = list("habits"),
            goals = list("goals"),
        )
    }

    /**
     * PC 叶子的 `details` 有**两种形状**，两种都得认：
     *
     * - `{"messages": [...], "future_notes": [...]}`（有 future_notes 时）
     * - 纯列表 `[...]`（没有 future_notes 时——**这是多数情况**）
     *
     * 只认前一种的话，大部分叶子的原文会被丢掉，用户看到的是
     * "它记得有这么回事，但说不出细节"。而 `--no-details` 导出时是 `null`。
     */
    private fun JsonElement?.toLeafDetails(): LeafDetails? = when (this) {
        is JsonArray -> LeafDetails(messages = toChatMessages())
        is JsonObject -> LeafDetails(
            messages = (this["messages"] as? JsonArray)?.toChatMessages().orEmpty(),
            futureNotes = (this["future_notes"] as? JsonArray)
                ?.mapNotNull { it.jsonPrimitive.contentOrNull }.orEmpty(),
        )
        else -> null
    }

    /**
     * 叶子里的原文消息。同样是两种形状（PC 兼容新旧 `messages_to_dict`）：
     * 旧版 `{"type":"human","content":"..."}`，新版 `{"type":"human","data":{...}}`。
     */
    private fun JsonArray.toChatMessages(): List<ChatMessage> = mapNotNull { element ->
        val obj = element as? JsonObject ?: return@mapNotNull null
        val type = obj["type"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
        val payload = obj["data"] as? JsonObject
        val content = (payload?.get("content") ?: obj["content"])
            ?.let { flattenContent(it) }.orEmpty()
        ChatMessage(role = normalizeRole(type), content = content)
    }

    // ── 对话 buffer（LangChain messages_to_dict 的形状）────────

    /**
     * buffer（`[{"type":"human","data":{...}}, ...]`）→ 安卓的消息行。
     *
     * **这里有两个曾经搞错、后果很严重的地方**：
     *
     * 1. **buffer 里混着 node 消息**（`data.metadata.node_id`，即"记忆摘要"）。
     *    它们必须按 node 消息导入（带 nodeId/nodeStart），否则会被当成普通原文：
     *    界面上变成一大堆摘要气泡，而且**下一次压缩会把它们再总结一遍**——
     *    表现就是"把 8/8 到 9/27 的全部内容压成了一个节点"。
     * 2. **每条消息都带原始时间戳**（`data.metadata.ts`）。丢掉它会让所有导入的消息
     *    变成"现在"，之后新建的记忆节点时间范围就全是错的。
     *
     * 认不出形状时**不猜**：记一条 skipped 并跳过。宁可少一条历史，
     * 也不要把 `data` 当成正文塞进去（那会污染记忆）。
     */
    private fun JsonElement?.toMessages(skipped: MutableList<String>): List<MessageEntity> {
        val arr = this as? JsonArray ?: return emptyList()
        val now = System.currentTimeMillis()
        return arr.mapIndexedNotNull { idx, element ->
            val obj = element as? JsonObject ?: return@mapIndexedNotNull null
            val type = obj["type"]?.jsonPrimitive?.contentOrNull
            val data = obj["data"] as? JsonObject
            if (type == null || data == null) {
                skipped += "buffer 第 ${idx + 1} 条结构不认识，已跳过"
                return@mapIndexedNotNull null
            }
            val role = when (type) {
                "human" -> "user"
                "ai" -> "assistant"
                "tool" -> "tool"
                "system" -> "system"
                else -> {
                    skipped += "buffer 第 ${idx + 1} 条角色 $type 不认识，已跳过"
                    return@mapIndexedNotNull null
                }
            }
            val metadata = data["metadata"] as? JsonObject
            val nodeId = metadata?.get("node_id")?.jsonPrimitive?.contentOrNull
                ?.takeIf { it.isNotBlank() }
            val nodeStart = metadata?.get("node_start")?.jsonPrimitive?.contentOrNull?.toIntOrNull()
            val ts = metadata?.get("ts")?.jsonPrimitive?.contentOrNull?.toEpochMillis()

            val content = data["content"]?.let { flattenContent(it) }.orEmpty()
            val calls = (data["tool_calls"] as? JsonArray)?.mapNotNull { it.toToolCall() }.orEmpty()
            MessageEntity(
                role = role,
                content = content,
                // 有原始时间就用它：没有的话新建的节点时间范围会全错
                tsMillis = ts ?: now,
                nodeId = nodeId,
                nodeStart = nodeStart,
                toolCalls = calls.takeIf { it.isNotEmpty() }
                    ?.let { PlannerJson.encodeToString(ListSerializer(ToolCall.serializer()), it) },
                toolCallId = data["tool_call_id"]?.jsonPrimitive?.contentOrNull,
                name = data["name"]?.jsonPrimitive?.contentOrNull,
            )
        }
    }

    /** LangChain 的 content 可能是字符串，也可能是 `[{type:text, text:...}]` 块数组。 */
    private fun flattenContent(element: JsonElement): String = when (element) {
        is JsonPrimitive -> element.contentOrNull.orEmpty()
        is JsonArray -> element.mapNotNull { block ->
            val obj = block as? JsonObject ?: return@mapNotNull null
            obj["text"]?.jsonPrimitive?.contentOrNull
        }.joinToString("\n")
        else -> ""
    }

    /** LangChain 的 tool_call：`{name, args: {...}, id}`。 */
    private fun JsonElement.toToolCall(): ToolCall? {
        val obj = this as? JsonObject ?: return null
        val name = obj["name"]?.jsonPrimitive?.contentOrNull ?: return null
        val id = obj["id"]?.jsonPrimitive?.contentOrNull
            ?: "call_${name}_${System.nanoTime()}"
        // args 正常是对象，但模型偶尔直接给字符串（那时它已经是 JSON 文本了，
        // 再 toString 会多包一层引号，下一轮请求就 400）
        val args = when (val raw = obj["args"]) {
            null, is JsonNull -> "{}"
            is JsonPrimitive -> raw.contentOrNull?.takeIf { it.isNotBlank() } ?: "{}"
            else -> raw.toString()
        }
        return ToolCall(id = id, function = ToolCallFunction(name = name, arguments = args))
    }

    private fun normalizeRole(role: String): String = when (role) {
        "human" -> "user"
        "ai" -> "assistant"
        else -> role
    }

    // ── 运行状态 ────────────────────────────────────────────

    private fun ExportBuffer.toState(
        characterId: String,
        nodes: List<ExportNode>,
    ): BufferStateEntity = BufferStateEntity(
        characterId = characterId,
        msgCounter = msgCounter,
        round = round,
        // 压缩进度按实际导入的节点数对齐：PC 那个计数是它自己库里的，
        // 直接搬过来会让新叶子从错误的序号起算
        compressedTotal = nodes.filter { it.level == 0 }.maxOfOrNull { it.roundEnd ?: 0 } ?: compressedTotal,
        lastActivityAt = lastActivityAt.toMillis(),
        nextHeartbeatAt = nextHeartbeatAt.toMillis(),
        heartbeatMinutes = heartbeatMinutes ?: 0.0,
        heartbeatNote = heartbeatNote.orEmpty(),
        lastPlayerMessageAt = lastPlayerMessageAt.toMillis(),
    )

    private fun Double?.toMillis(): Long = this?.let { (it * 1000).toLong() } ?: 0L

    // ── 日历 ────────────────────────────────────────────────

    private fun ExportCalendarItem.toEntity(skipped: MutableList<String>): CalendarItemEntity? {
        if (date.isBlank()) {
            skipped += "日历项 ${content.ifBlank { "#$id" }} 没有日期，已跳过"
            return null
        }
        return CalendarItemEntity(
            // id 不沿用：安卓库是自增主键，沿用会跟已有行撞车。
            // 重复系列靠 seriesId 串起来，不依赖 id。
            id = 0,
            date = date,
            time = time?.takeIf { it.isNotBlank() },
            kind = kind.takeIf { it in CalendarItemEntity.KINDS } ?: CalendarItemEntity.KIND_EVENT,
            content = content,
            detail = detail,
            status = status.takeIf { it in CalendarItemEntity.STATUSES } ?: CalendarItemEntity.STATUS_TODO,
            remindedAt = remindedAt,
            createdAt = createdAt.toEpochMillis() ?: System.currentTimeMillis(),
            doneAt = doneAt.toEpochMillis(),
            seriesId = seriesId?.takeIf { it.isNotBlank() },
            repeat = repeat?.takeIf { it in CalendarItemEntity.REPEAT_RULES },
            repeatUntil = repeatUntil,
        )
    }

    /** PC 存的是 `yyyy-MM-dd HH:mm:ss` 文本（本地时区），不是 epoch。 */
    private fun String?.toEpochMillis(): Long? {
        val text = this?.trim().orEmpty()
        if (text.isEmpty()) return null
        return runCatching {
            LocalDateTime.parse(text, PC_TIME)
                .atZone(ZoneId.of("Asia/Shanghai"))
                .toInstant()
                .toEpochMilli()
        }.getOrNull()
    }
}