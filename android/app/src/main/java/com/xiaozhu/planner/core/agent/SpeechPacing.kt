package com.xiaozhu.planner.core.agent

/**
 * 「上一段说了多少字」——`continue_speaking` 的分段节奏靠它算（PC 版存在 session 上）。
 *
 * 单开一个类而不是全局变量：它是有状态、按轮变化的会话事实，
 * 而且注入之后单测不用去猜暂停多久。
 */
class SpeechPacing {

    /** 上一段文本的字数（AgentLoop 在进入工具调用前记下）。 */
    @Volatile
    var lastSegmentChars: Int = 0

    /**
     * 暂停秒数：基础 2 秒 + 每 10 字加 1 秒，上限 10 秒（防呆）。
     * 公式照抄 PC 版 `session.pause_before_continue`——两端的说话节奏要一致。
     */
    val pauseSeconds: Double
        get() = (BASE_SECONDS + lastSegmentChars / 10.0).coerceAtMost(MAX_SECONDS)

    private companion object {
        const val BASE_SECONDS = 2.0
        const val MAX_SECONDS = 10.0
    }
}
