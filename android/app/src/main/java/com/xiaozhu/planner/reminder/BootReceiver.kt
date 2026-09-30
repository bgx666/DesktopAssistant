package com.xiaozhu.planner.reminder

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.data.PrefsDndStore
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * 系统事件后把状态接回来：闹钟在重启后会**全部丢失**（AlarmManager 不持久化），
 * 时区/时间被改动后已排的时刻也不再正确。所以这几个广播都必须重排一次。
 *
 * 顺带做两件"启动时才做也不亏"的事：
 * - **补提醒**：关机/被杀期间到点的安排，开机后补一条通知，别让它悄悄沉底；
 * - **重复系列续期**：只能生成到 horizon，得有东西定期续（PC 版放在调度线程里按小时查）。
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action !in HANDLED) return

        val pending = goAsync()
        val appContext = context.applicationContext
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                val store = CalendarStore(PlannerDatabase.get(appContext).calendar())
                val scheduler = ReminderScheduler(appContext, store, PrefsDndStore(appContext))
                ReminderNotifications.ensureChannel(appContext)
                store.extendAllSeries()
                catchUp(appContext, store, scheduler)
                scheduler.sync()
            } catch (_: Exception) {
                // 接不回来也不该崩；下一次启动再试
            } finally {
                pending.finish()
            }
        }
    }

    private suspend fun catchUp(context: Context, store: CalendarStore, scheduler: ReminderScheduler) {
        val dnd = PrefsDndStore(context)
        val overdue = store.overdue()
        if (overdue.isEmpty()) return
        if (com.xiaozhu.planner.core.calendar.Dnd.inDnd(CalendarDates.now(), dnd.load())) {
            // 免打扰里不打扰，等窗口结束再补（这里只把标记留空，由下次 sync 接手）
            return
        }
        val lines = overdue.take(MAX_LINES).map {
            "「${it.content}」（原定 ${it.date} ${it.time ?: "全天"}）"
        }
        overdue.forEach { store.markReminded(it.id) }
        ReminderNotifications.post(context, NOTIFICATION_ID, lines, late = true)
        ReminderEvents.emit(lines)
    }

    companion object {
        private const val MAX_LINES = 3
        private const val NOTIFICATION_ID = 90001

        private val HANDLED = setOf(
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            Intent.ACTION_TIME_CHANGED,
            Intent.ACTION_TIMEZONE_CHANGED,
            Intent.ACTION_DATE_CHANGED,
        )
    }
}
