package com.xiaozhu.planner.ui.chat

/** 聊天页的展示模型（与协议层的 ChatMessage 分开：UI 不该知道 tool_calls 的细节）。 */
data class UiMessage(
    val id: Long,
    val fromUser: Boolean,
    val text: String = "",
    val tools: List<UiTool> = emptyList(),
    val failed: Boolean = false,
    /** 系统触发的一条（到点提醒等）——居中显示，不要伪装成用户说的话。 */
    val fromSystem: Boolean = false,
)

data class UiTool(
    val name: String,
    val args: String,
    val result: String? = null,
)

data class ChatUiState(
    val messages: List<UiMessage> = emptyList(),
    val streaming: Boolean = false,
    val configured: Boolean = false,
    val hint: String? = null,
    /** 一行小提示：例如"已把 40 条对话压成一个记忆节点"。 */
    val memoryNotice: String? = null,
)

/** 记忆浏览页的概览数据。 */
data class MemoryStats(
    val totalNodes: Int,
    val levels: List<Pair<Int, Int>>,
    val rootId: String?,
    val rawMessages: Int,
)