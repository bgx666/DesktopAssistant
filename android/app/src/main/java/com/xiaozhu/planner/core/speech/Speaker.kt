package com.xiaozhu.planner.core.speech

/**
 * 朗读接口。UI 只依赖它，具体实现（MiMo 云 TTS / 系统 TTS / 测试里的假实现）
 * 在 app 层装配。
 */
interface Speaker {

    /** 现在能不能朗读（没配置、没权限、引擎缺失时为 false）。 */
    val available: Boolean

    /** 异步朗读一段文本（实现自己负责清洗、合成、播放）。 */
    fun speak(text: String)

    /** 立刻停下（用户发新消息、点停止时调）。 */
    fun stop()

    /** 正在出声吗（UI 用来显示"停止朗读"）。 */
    fun isSpeaking(): Boolean
}
