package com.xiaozhu.planner.ui.chat

/** 聊天页的展示模型（与协议层的 ChatMessage 分开：UI 不该知道 tool_calls 的细节）。 */
data class UiMessage(
    val id: Long,
    val fromUser: Boolean,
    val text: String = "",
    val tools: List<UiTool> = emptyList(),
    val failed: Boolean = false,
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
)