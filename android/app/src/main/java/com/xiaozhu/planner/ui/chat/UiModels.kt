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
    /**
     * 这条消息带的图片（data URL）。
     *
     * 与 PC 版一致：图片常驻 buffer、每轮模型调用重发；压缩成记忆节点时
     * 换成"（图片）"占位，base64 不进记忆树。
     */
    val images: List<String> = emptyList(),
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
    /**
     * 已经选好、还没发出去的图片（data URL）。发送时随消息一起走，发完清空。
     */
    val pendingImages: List<String> = emptyList(),

    /** 待发的附件正文（纯文本/docx 读出来的，发送时并进消息文本）。 */
    val pendingText: String? = null,

    /** 待发附件的显示名（界面提示"已附加：xxx"）。 */
    val pendingDocName: String? = null,

    /** 逾期未完成的数量（PC 版"日历"页签上的角标）。 */
    val overdueCount: Int = 0,

    /**
     * 包里带了旧数据但还没导（库非空，自动路径跳过了）→ 界面问一句。
     *
     * 静默跳过是错的：用户根本不知道包里带着旧数据、也不知道自己错过了什么。
     */
    val seedPrompt: SeedPrompt? = null,
)

/** "包里带了电脑上的旧数据，要导入吗"那张卡片的内容。 */
data class SeedPrompt(
    val nodes: Int,
    val calendar: Int,
    val currentNodes: Int,
    val currentMessages: Int,
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
 * 回读历史时怎么处理"刚生成完、还在内存里的那些气泡"。
 *
 * 踩过两次坑，第二次比第一次隐蔽：
 * 1. 无条件把内存里的回复接在回读结果后面——而它**已经落盘了** → 同一条回复出现两次
 * 2. 改成"按文本比对"之后，**一轮里模型调了两次**的情况又漏了：内存里那条是两个
 *    模型输出**拼在一起**的（"…今天真的开个头。傍晚我再来问一句。"），跟库里任何
 *    单条都不相等 → 判定"库里没有" → 又接了一遍（用户看到一模一样的回复两遍）
 *
 * 所以判据换成**不看文本、只看事实**：这一轮有没有新原文落进库。
 * 落进去了（`rawAfter > rawBefore`）就说明落盘成功，内存那份纯属多余，丢掉；
 * 没落进去才是落盘失败，那时才把它接上（否则用户会觉得"它没回我"）。
 */
internal object HistoryMerge {

    fun merge(
        built: List<UiMessage>,
        tail: UiMessage?,
        rawBefore: Int?,
        rawAfter: Int,
    ): List<UiMessage> {
        if (tail == null) return built
        val persisted = rawBefore != null && rawAfter > rawBefore
        return if (persisted) built else built + tail
    }
}
