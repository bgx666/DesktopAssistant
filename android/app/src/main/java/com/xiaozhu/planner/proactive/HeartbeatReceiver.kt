package com.xiaozhu.planner.proactive

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.core.tools.HeartbeatStore
import com.xiaozhu.planner.data.PrefsDndStore
import com.xiaozhu.planner.data.PrefsHeartbeatStore
import com.xiaozhu.planner.reminder.ReminderNotifications

/**
 * 心跳闹钟响了。
 *
 * 三条去向（决策在 [HeartbeatDecisions]，这里只负责执行）：
 * - 界面活着 → 发个事件，界面自己说（用户当场看得见）
 * - 界面不在 → 起 [HeartbeatService] 在后台跑一轮，把回复推成通知
 * - 免打扰 → 顺延到窗口结束（跳过一次心跳没损失，吵醒用户才是损失）
 *
 * 起前台服务可能被系统拒绝（Android 12+ 对后台起服务有限制，靠精确闹钟权限豁免）。
 * 被拒时**退回一条通知**而不是静默失败——用户至少知道"它想说话但没说出来"。
 */
class HeartbeatReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_HEARTBEAT) return

        val appContext = context.applicationContext
        val note = PrefsHeartbeatStore(appContext).current()?.second.orEmpty()

        val decision = HeartbeatDecisions.decide(
            inDnd = Dnd.inDnd(CalendarDates.now(), PrefsDndStore(appContext).load()),
            dndEndMillis = dndEndMillis(appContext),
            uiAlive = AppPresence.uiAlive,
            nowMillis = System.currentTimeMillis(),
        )

        when (decision) {
            HeartbeatDecision.Skip -> Unit

            is HeartbeatDecision.Defer -> HeartbeatScheduler(appContext, PrefsHeartbeatStore(appContext))
                .armAt(decision.atMillis)

            HeartbeatDecision.ToUi -> HeartbeatEvents.emit(note)

            HeartbeatDecision.ToBackground -> startBackgroundRound(appContext, note)
        }
    }

    private fun startBackgroundRound(context: Context, note: String) {
        val started = runCatching {
            ContextCompat.startForegroundService(context, HeartbeatService.intent(context, note))
            true
        }.getOrDefault(false)

        if (!started) {
            // 起不来也不能静默：至少告诉用户"它到点了，但没能说话"
            ReminderNotifications.post(
                context = context,
                id = HeartbeatService.NOTIFICATION_ID,
                lines = listOf(if (note.isBlank()) "我该醒一次了，但没能起来。" else "醒来要做：$note"),
                title = "小助到点了",
            )
        }
    }

    /** 免打扰窗口何时结束（0 = 算不出来）。 */
    private fun dndEndMillis(context: Context): Long {
        val settings = PrefsDndStore(context).load()
        settings.untilEpochMillis?.let { if (it > System.currentTimeMillis()) return it }
        val now = CalendarDates.now()
        if (!Dnd.inDnd(now, settings)) return 0L
        return Dnd.untilHour(now, Dnd.DEFAULT_END_HOUR)
    }

    companion object {
        const val ACTION_HEARTBEAT = "com.xiaozhu.planner.action.HEARTBEAT"

        /** 供 [HeartbeatStore] 之外的代码查询（调试/界面显示"下次醒来"）。 */
        fun describeNextWake(store: HeartbeatStore): String? {
            val (at, note) = store.current() ?: return null
            val left = at - System.currentTimeMillis()
            if (left <= 0) return null
            val clock = HeartbeatScheduler.stamp(at)
            return if (note.isBlank()) "下次醒来 $clock" else "下次醒来 $clock（$note）"
        }
    }
}
