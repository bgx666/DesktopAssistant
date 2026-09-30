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
            if (level == 0 && details == null && summary.isNotBlank()) {
                skipped += "节点 $id 没有原文（导出时用了 --no-details）"
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

    /** PC 叶子的 `details` = `{messages: [...], future_notes: [...]}`（安卓同构）。 */
    private fun JsonElement?.toLeafDetails(): LeafDetails? {
        val obj = this as? JsonObject ?: return null
        val raw = obj["messages"] as? JsonArray ?: return null
        val msgs = raw.mapNotNull { element ->
            val m = element as? JsonObject ?: return@mapNotNull null
            val role = m["role"]?.jsonPrimitive?.contentOrNull ?: "user"
            val content = m["content"]?.jsonPrimitive?.contentOrNull.orEmpty()
            ChatMessage(role = normalizeRole(role), content = content)
        }
        val notes = (obj["future_notes"] as? JsonArray)
            ?.mapNotNull { it.jsonPrimitive.contentOrNull }.orEmpty()
        return LeafDetails(messages = msgs, futureNotes = notes)
    }

    // ── 对话 buffer（LangChain messages_to_dict 的形状）────────

    /**
     * `[{"type":"human","data":{"content":"..."}}, ...]` → 安卓的消息行。
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
            val content = data["content"]?.let { flattenContent(it) }.orEmpty()
            val calls = (data["tool_calls"] as? JsonArray)?.mapNotNull { it.toToolCall() }.orEmpty()
            MessageEntity(
                role = role,
                content = content,
                tsMillis = now,
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
        val args = obj["args"]?.takeIf { it !is JsonNull }?.toString() ?: "{}"
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
