package com.xiaozhu.planner.reminder

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.data.PrefsDndStore
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_TODO
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * 闹钟到点 → 标 remindedAt + 弹通知。
 *
 * 免打扰期间**不投递也不标记**，而是把这个闹钟顺延到免打扰结束——
 * 这是 PC 版"等 DND 结束再补"的等价做法：手机上不这么做的话，
 * 提醒会在 22:30 被静默丢掉，用户永远不知道有这回事。
 *
 * 用 `goAsync()` 而不是 `runBlocking`：广播接收器的生命周期要以 `finish()` 明确结束，
 * 而这中间要异步读库。
 */
class ReminderReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_FIRE) return
        val itemId = intent.getLongExtra(EXTRA_ITEM_ID, 0L)
        if (itemId <= 0L) return
        val scheduledAt = intent.getLongExtra(EXTRA_SCHEDULED_AT, 0L)
        val deferred = intent.getBooleanExtra(EXTRA_DEFERRED, false)

        val pending = goAsync()
        val appContext = context.applicationContext
        CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
            try {
                deliver(appContext, itemId, scheduledAt, deferred)
            } catch (_: Exception) {
                // 提醒失败不该让进程崩：下一次 sync / 补提醒还有机会
            } finally {
                pending.finish()
            }
        }
    }

    companion object {
        const val ACTION_FIRE = "com.xiaozhu.planner.action.REMINDER_FIRE"
        const val EXTRA_ITEM_ID = "item_id"
        const val EXTRA_SCHEDULED_AT = "scheduled_at"
        const val EXTRA_DEFERRED = "deferred"

        /**
         * @param scheduledAt 这个闹钟当初排的时刻（0 = 未知，按可信处理）
         * @param deferred    是否是从免打扰顺延过来的（顺延的 AT 本来就不等于 item 的时刻）
         */
        suspend fun deliver(context: Context, itemId: Long, scheduledAt: Long = 0L, deferred: Boolean = false) {
            val store = CalendarStore(PlannerDatabase.get(context).calendar())
            val item = store.byId(itemId)
            val dndStore = PrefsDndStore(context)
            val scheduler = ReminderScheduler(context, store, dndStore)
            val now = System.currentTimeMillis()

            val decision = ReminderDecisions.decide(
                item = item,
                scheduledAt = scheduledAt,
                deferred = deferred,
                inDnd = Dnd.inDnd(CalendarDates.now(), dndStore.load()),
                dndEndMillis = scheduler.dndEndMillis(),
                nowMillis = now,
            )

            when (decision) {
                ReminderDecision.Skip -> Unit
                is ReminderDecision.Reschedule -> scheduler.scheduleAt(
                    itemId,
                    decision.atMillis,
                    deferred = decision.deferred,
                )
                is ReminderDecision.Deliver -> {
                    store.markReminded(itemId)
                    ReminderNotifications.post(context, itemId.toInt(), listOf(decision.line))
                    ReminderEvents.emit(listOf(decision.line))
                }
            }
        }
    }
}
