package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** 用户画像四维度（与 PC 版一致）。 */
@Serializable
data class ProfileInfo(
    val preferences: List<String> = emptyList(),
    val personality: List<String> = emptyList(),
    val habits: List<String> = emptyList(),
    val goals: List<String> = emptyList(),
) {
    val isEmpty: Boolean
        get() = preferences.isEmpty() && personality.isEmpty() && habits.isEmpty() && goals.isEmpty()

    fun merge(other: ProfileInfo) = ProfileInfo(
        preferences = (preferences + other.preferences).distinct(),
        personality = (personality + other.personality).distinct(),
        habits = (habits + other.habits).distinct(),
        goals = (goals + other.goals).distinct(),
    )
}

/** 叶子节点里保存的原文 + 后续说明。 */
@Serializable
data class LeafDetails(
    val messages: List<ChatMessage> = emptyList(),
    @SerialName("future_notes") val futureNotes: List<String> = emptyList(),
)

/** 压缩的产出（结构化输出，与 PC 版 `MemoryNodeOutput` 对齐）。 */
@Serializable
data class MemoryNodeOutput(
    val summary: String = "",
    val profile: ProfileInfo = ProfileInfo(),
    @SerialName("future_notes") val futureNotes: List<String> = emptyList(),
    val meta: JsonObject = JsonObject(emptyMap()),
)

/** 展开节点时给模型/UI 的信息。 */
data class NodeInfo(
    val nodeId: String,
    val level: Int,
    val summary: String,
    val timeRange: Pair<String, String>?,
    val profile: ProfileInfo?,
    val futureNotes: List<String> = emptyList(),
    val details: List<ChatMessage> = emptyList(),
    val children: List<NodeInfo> = emptyList(),
)

/** 检索命中。 */
data class MemoryHit(
    val nodeId: String,
    val level: Int,
    val roundStart: Int?,
    val timeStart: String?,
    val snippet: String,
)

/** 记忆树的读写门面（纯 Kotlin + Room DAO，便于单测）。 */
object MemoryJson {
    val json = PlannerJson

    fun encodeDetails(details: LeafDetails): String = json.encodeToString(LeafDetails.serializer(), details)
    fun decodeDetails(raw: String?): LeafDetails =
        raw?.let { runCatching { json.decodeFromString(LeafDetails.serializer(), it) }.getOrNull() } ?: LeafDetails()

    fun encodeProfile(profile: ProfileInfo): String = json.encodeToString(ProfileInfo.serializer(), profile)
    fun decodeProfile(raw: String?): ProfileInfo? =
        raw?.let { runCatching { json.decodeFromString(ProfileInfo.serializer(), it) }.getOrNull() }

    fun encodeOutput(out: MemoryNodeOutput): String = json.encodeToString(MemoryNodeOutput.serializer(), out)
    fun decodeOutput(raw: String): MemoryNodeOutput? =
        runCatching { json.decodeFromString(MemoryNodeOutput.serializer(), raw) }.getOrNull()
}