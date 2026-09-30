package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.memory.BufferRepair
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.MessageMapping
import com.xiaozhu.planner.data.db.BufferStateEntity
import com.xiaozhu.planner.data.db.MessageEntity

/**
 * 对话 buffer 的落盘与恢复（Room 是唯一事实源；内存里的历史只是它的一个视图）。
 *
 * 顺序很重要：**节点消息在前、原始消息在后** —— 节点代表更早的对话。
 * 恢复时统一过一遍 [BufferRepair]，把上次被杀留下的悬空 tool_calls 清掉。
 */
class ConversationService(
    private val store: MemoryStore,
    private val characterId: String = MemoryStore.DEFAULT_CHARACTER,
) {

    suspend fun loadHistory(): List<ChatMessage> {
        val nodes = store.nodeMessages().map { MessageMapping.toChatMessage(it) }
        val raw = store.rawMessages().map { MessageMapping.toChatMessage(it) }
        return BufferRepair.repair(nodes + raw)
    }

    /** 把一批消息顺序落盘（保持 assistant(tool_calls) 与其 tool 结果的相邻关系）。 */
    suspend fun persist(messages: List<ChatMessage>, tsMillis: Long) {
        messages.filter { it.role != "system" }.forEach { message ->
            store.appendMessage(
                role = message.role,
                content = message.content.orEmpty(),
                tsMillis = tsMillis,
                toolCalls = MessageMapping.toEntity(message, tsMillis).toolCalls,
                toolCallId = message.toolCallId,
                name = message.name,
            )
        }
    }

    suspend fun loadState(): BufferStateEntity =
        store.loadBufferState() ?: BufferStateEntity(characterId = characterId)

    suspend fun saveState(state: BufferStateEntity) = store.saveBufferState(state.copy(characterId = characterId))

    /** 撤销用：按 seq 顺序取全部**原文**（不含记忆节点消息）。 */
    suspend fun rawMessagesForUndo(): List<MessageEntity> = store.rawMessages()

    /** 撤销用：删掉这些原文。 */
    suspend fun deleteMessages(seqs: List<Long>) = store.deleteMessages(seqs)

    /**
     * 撤回第 [rawIndex] 条**原文及其之后**的全部对话（PC 版 `/undo` 同义）。
     *
     * 只动原文，**不动记忆节点**——那些内容已经压进长期记忆树了，
     * 删掉它等于删记忆，而不是"撤回刚说的一句话"。
     *
     * 返回是否真的删掉了东西（界面据此决定要不要把内容放回输入框）。
     */
    suspend fun truncateFrom(rawIndex: Int): Boolean {
        val raw = store.rawMessages()
        if (rawIndex !in raw.indices) return false
        val doomed = raw.drop(rawIndex)
        if (doomed.isEmpty()) return false
        store.deleteMessages(doomed.map { it.seq })
        return true
    }
}