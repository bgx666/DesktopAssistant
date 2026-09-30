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
    /**
     * 这一段对话里出现过的图片的**引用**（本体在文件里，见 ImageStore）。
     *
     * 压缩时图片本体不进记忆树（base64 会把节点撑大几百倍），但"那天有张天空的照片"
     * 这件事必须留下来——否则以后问"那天的天空什么样"就永远答不出。
     * 留着引用 + 时间 + 当时的话，模型就能用 `list_images` / `show_image` 把它找回来。
     */
    @SerialName("image_refs") val imageRefs: List<ImageRef> = emptyList(),
)

/** 记忆节点里留下的一张图片的索引（不含图片本体）。 */
@Serializable
data class ImageRef(
    /** `sha1:<hex>` 形式的引用。 */
    val ref: String,
    /** 这张图是什么时候发的（`yyyy-MM-dd HH:mm:ss`）。 */
    val ts: String,
    /** 发图时用户说的话（检索线索；空就是只发了图没说话）。 */
    @SerialName("said") val said: String = "",
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
    /**
     * 这个节点里的图片（叶子才有）。
     *
     * 为什么要带出来：叶子原文里图片的位置是"（图片）"占位——
     * 模型看到占位却不知道**是哪张**，就没法调 `show_image`。
     * 把 id 一起给它，"搜记忆 → 翻到叶子 → 顺手看图"才是一条连贯的路。
     */
    val images: List<NodeImageRef> = emptyList(),
)

/** 节点里的一张图（给模型看的索引，含可直接传给 `show_image` 的 id）。 */
data class NodeImageRef(
    val id: String,
    val ts: String,
    val said: String,
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