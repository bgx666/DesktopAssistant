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

/** 工具调用卡片的数据。 */
data class UiTool(
    val name: String,
    val args: String,
    val result: String? = null,
    /**
     * 是不是"正在执行"的那张卡（PC 版实时卡片转圈 + 「正在执行 X」，历史重放的直接是完成态）。
     * 历史里的卡片没有结果文本（结果在后面的 tool 消息里），所以不能靠 result 判断。
     */
    val live: Boolean = false,
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
    /**
     * 上下文 token 估算（PC 版标题栏那一行）。
     * 算法照抄 PC：`chars × 1.2`，超过 60000 变红。
     */
    val contextTokens: Int = 0,
    /** 下次主动醒来的倒计时文案（PC 版标题栏第二行）。 */
    val heartbeatLabel: String? = null,
    /** 逾期未完成的数量（PC 版"日历"页签上的角标）。 */
    val overdueCount: Int = 0,
)

/**
 * 刚发出去的那条消息，在"原文"里排第几 —— 撤销要靠它定位。
 *
 * 踩过的坑：新消息不带这个序号，而界面又只在有序号时才画「撤销」，
 * 于是**刚发完那几秒（回复还在流式输出）按钮是消失的**，等这一轮结束回读数据库才出现。
 *
 * 算法：界面上的原文气泡是按库里的顺序排的，所以"已有的原文条数"就是新消息的序号。
 * 历史还没载入时算不出来，返回 null（宁可暂时不给按钮，也不能给错序号——
 * 撤错位置会删掉不该删的对话）。
 */
internal fun rawIndexOfNewMessage(messages: List<UiMessage>, historyLoaded: Boolean): Int? =
    if (!historyLoaded) null else messages.count { it.rawIndex != null }

/** 记忆浏览页的概览数据。 */
data class MemoryStats(
    val totalNodes: Int,
    val levels: List<Pair<Int, Int>>,
    val rootId: String?,
    val rawMessages: Int,
)

/**
 * 回读历史时怎么处理"刚生成完、还在内存里的那一条"。
 *
 * 踩过的坑：一轮结束后无条件把内存里的回复接在回读结果后面，
 * 而它**已经落盘了**——于是同一条回复在界面上出现两次
 * （用户只问了一句，却收到两个一模一样的回答）。
 *
 * 所以只在回读结果里**确实没有**它时才接上（那种情况意味着落盘失败，
 * 不接就会让用户觉得"它没回我"）。
 */
internal object HistoryMerge {

    fun merge(built: List<UiMessage>, tail: UiMessage?): List<UiMessage> {
        if (tail == null) return built
        val text = tail.text.trim()
        val alreadyThere = text.isNotEmpty() && built.any {
            it.kind == UiMessageKind.Assistant && it.text.trim() == text
        }
        return if (alreadyThere) built else built + tail
    }
}
