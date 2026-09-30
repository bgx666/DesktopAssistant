package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.data.db.MessageEntity
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer

/** Room 行 ↔ 协议层消息。工具调用与图片都要能往返（恢复上下文后仍能配对/重发）。 */
object MessageMapping {

    /**
     * 库里存的**原始**图片值：可能是新格式引用（`sha1:…`），也可能是老格式的
     * base64 data URL（迁移之前存进去的）。**两种都要认**。
     */
    fun rawImagesOf(row: MessageEntity): List<String> = row.images?.let { raw ->
        runCatching {
            PlannerJson.decodeFromString(ListSerializer(String.serializer()), raw)
        }.getOrNull()
    }.orEmpty()

    /** 界面/模型要的图片（把引用解析成 data URL；解析不出来的跳过）。 */
    fun imagesOf(row: MessageEntity): List<String> =
        com.xiaozhu.planner.core.image.ImageStore.dataUrls(rawImagesOf(row))

    fun toChatMessage(row: MessageEntity): ChatMessage = ChatMessage(
        role = row.role,
        content = row.content,
        toolCalls = row.toolCalls?.let { raw ->
            runCatching {
                PlannerJson.decodeFromString(ListSerializer(ToolCall.serializer()), raw)
            }.getOrNull()
        },
        toolCallId = row.toolCallId,
        name = row.name,
        images = imagesOf(row),
    )

    /**
     * 存进库时**尽量只存引用**（图片本体进文件）。
     *
     * 存文件失败就退回 base64 —— 绝不能因为"存文件失败"把图弄丢，
     * 那种行下次启动会被迁移重试。
     */
    fun toEntity(message: ChatMessage, tsMillis: Long, nodeId: String? = null, nodeStart: Int? = null): MessageEntity {
        val stored = message.images.map { value ->
            if (com.xiaozhu.planner.core.image.ImageStore.isInline(value)) {
                com.xiaozhu.planner.core.image.ImageStore.put(value) ?: value
            } else {
                value
            }
        }
        return MessageEntity(
            role = message.role,
            content = message.content.orEmpty(),
            tsMillis = tsMillis,
            nodeId = nodeId,
            nodeStart = nodeStart,
            toolCalls = message.toolCalls?.takeIf { it.isNotEmpty() }?.let { calls ->
                PlannerJson.encodeToString(ListSerializer(ToolCall.serializer()), calls)
            },
            toolCallId = message.toolCallId,
            name = message.name,
            images = stored.takeIf { it.isNotEmpty() }?.let { urls ->
                PlannerJson.encodeToString(ListSerializer(String.serializer()), urls)
            },
        )
    }
}