package com.xiaozhu.planner.reminder

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow

/**
 * 提醒 → 界面的一条通道：闹钟响了，如果 App 正开着，就让模型说句话
 * （等价于 PC 版"注入一条 `（系统：…）` 消息并唤醒一次"）。
 *
 * 为什么用 SharedFlow 而不是 LiveData/广播：闹钟在 Receiver 里被触发，
 * 与 UI 生命周期无关；`tryEmit` 不阻塞，App 没开时没人收也不会出问题
 * （那种情况下用户看到的就是通知栏本身）。
 */
object ReminderEvents {

    private val _fired = MutableSharedFlow<List<String>>(extraBufferCapacity = 8)

    /** 每条是一行「「内容」（原定 2026-09-30 20:00）」形式的描述。 */
    val fired: SharedFlow<List<String>> = _fired

    fun emit(lines: List<String>) {
        if (lines.isNotEmpty()) _fired.tryEmit(lines)
    }
}
