package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.data.db.MessageEntity
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer

/** Room 行 ↔ 协议层消息。工具调用与图片都要能往返（恢复上下文后仍能配对/重发）。 */
object MessageMapping {

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
        images = row.images?.let { raw ->
            runCatching {
                PlannerJson.decodeFromString(ListSerializer(String.serializer()), raw)
            }.getOrNull()
        }.orEmpty(),
    )

    /** 从库里读回一条消息的图片（data URL 列表）。读不出来就当没有，不炸。 */
    fun imagesOf(row: MessageEntity): List<String> = row.images?.let { raw ->
        runCatching {
            PlannerJson.decodeFromString(ListSerializer(String.serializer()), raw)
        }.getOrNull()
    }.orEmpty()

    fun toEntity(message: ChatMessage, tsMillis: Long, nodeId: String? = null, nodeStart: Int? = null): MessageEntity =
        MessageEntity(
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
            images = message.images.takeIf { it.isNotEmpty() }?.let { urls ->
                PlannerJson.encodeToString(ListSerializer(String.serializer()), urls)
            },
        )
}