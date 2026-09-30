package com.xiaozhu.planner.proactive

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow

/**
 * 心跳到点 → 界面的一条通道（与 `ReminderEvents` 同一个套路）。
 *
 * 界面活着时走这里（用户能当场看到它在说话）；界面不在时由
 * `HeartbeatService` 自己跑一轮并推通知。
 */
object HeartbeatEvents {

    private val _fired = MutableSharedFlow<String>(extraBufferCapacity = 4)

    /** 载荷是模型设心跳时留的 note（可能为空）。 */
    val fired: SharedFlow<String> = _fired

    fun emit(note: String) {
        _fired.tryEmit(note)
    }
}

/**
 * 「App 现在活着吗」——决定心跳到点走哪条路。
 *
 * 为什么不用进程存活判断：进程活着不等于界面在（可能只是个后台缓存进程），
 * 而这两条路的行为差别很大（界面上说 vs 推通知）。所以由界面自己举手。
 */
object AppPresence {

    @Volatile
    var foreground: Boolean = false

    /** 界面存活（ViewModel 在）——心跳可以由界面处理。 */
    @Volatile
    var uiAlive: Boolean = false
}
