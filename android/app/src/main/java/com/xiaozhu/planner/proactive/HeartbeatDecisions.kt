package com.xiaozhu.planner.proactive

/**
 * 心跳到点之后该怎么办 —— **纯函数**，不碰 AlarmManager / 服务 / 通知。
 *
 * 与提醒的决策（`ReminderDecisions`）分开：两者的语义不同。
 * 提醒是**定点承诺**，到点必须让用户看见；心跳是**自省节律**，
 * 免打扰里可以整个跳过（跳过一次心跳没有损失，吵醒用户才是损失）。
 */
sealed interface HeartbeatDecision {
    /** 界面活着：交给界面说（用户能当场看到）。 */
    object ToUi : HeartbeatDecision

    /** 界面不在：起后台服务自己跑一轮，结果推通知。 */
    object ToBackground : HeartbeatDecision

    /** 免打扰：顺延到窗口结束。 */
    data class Defer(val atMillis: Long) : HeartbeatDecision

    /** 什么都不做（免打扰且算不出结束时刻）。 */
    object Skip : HeartbeatDecision
}

object HeartbeatDecisions {

    fun decide(
        inDnd: Boolean,
        dndEndMillis: Long,
        uiAlive: Boolean,
        nowMillis: Long,
    ): HeartbeatDecision {
        if (inDnd) {
            return if (dndEndMillis > nowMillis) {
                HeartbeatDecision.Defer(dndEndMillis)
            } else {
                HeartbeatDecision.Skip
            }
        }
        return if (uiAlive) HeartbeatDecision.ToUi else HeartbeatDecision.ToBackground
    }
}
