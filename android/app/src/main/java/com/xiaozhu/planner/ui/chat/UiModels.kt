package com.xiaozhu.planner.ui.chat

/** 一条消息在界面上属于哪一类（决定气泡样式，与 PC 版的 `.msg.*` 一一对应）。 */
enum class UiMessageKind {
    /** 用户说的（右侧蓝色渐变气泡）。 */
    User,

    /** 小助说的（左侧浅色气泡 + 头像）。 */
    Assistant,

    /** 系统触发的（到点提醒、心跳）——居中灰字，PC 版叫 `.msg.log`。 */
    System,

    /** 记忆节点（压缩后的摘要）——顶部虚线卡片，PC 版叫 `.msg.memory`。 */
    Memory,
}

/** 聊天页的展示模型（与协议层的 ChatMessage 分开：UI 不该知道 tool_calls 的细节）。 */
data class UiMessage(
    val id: Long,
    val kind: UiMessageKind,
    val text: String = "",
    val tools: List<UiTool> = emptyList(),
    val failed: Boolean = false,
    /** 时刻（HH:mm），PC 版每条下面会显示时间。 */
    val time: String? = null,
    /**
     * 这条对应第几条**原始消息**（0 起，按 seq 顺序）。
     *
     * 撤销要用它：撤回某条 = 删掉它及其之后的原文。记忆卡片没有这个值
     * （它代表的是已经被压缩掉的历史，不能被撤销）。
     */
    val rawIndex: Int? = null,
    /** 记忆卡片的元信息（层级、时间范围），仅 [UiMessageKind.Memory] 有。 */
    val memoryMeta: String? = null,
) {
    val fromUser: Boolean get() = kind == UiMessageKind.User
}

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
    /** 记忆概览：让"它记得"这件事在界面上一眼可见。 */
    val memory: MemoryStats = MemoryStats(0, emptyList(), null, 0),
    /** 撤销后要把内容放回输入框（PC 版行为：方便改完重发，而不是直接丢掉）。 */
    val restoreDraft: String? = null,
    /** 历史是否已经载入界面（决定要不要显示"正在读取…"）。 */
    val historyLoaded: Boolean = false,
)

/** 记忆浏览页的概览数据。 */
data class MemoryStats(
    val totalNodes: Int,
    val levels: List<Pair<Int, Int>>,
    val rootId: String?,
    val rawMessages: Int,
)
