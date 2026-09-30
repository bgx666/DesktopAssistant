package com.xiaozhu.planner.reminder

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.core.calendar.DndStore
import com.xiaozhu.planner.core.calendar.ReminderPlanner
import com.xiaozhu.planner.data.db.CalendarItemEntity

/**
 * 到点提醒的调度：把"未来的、带时刻的待办项"逐条登记成精确闹钟。
 *
 * 为什么用 AlarmManager 而不是 WorkManager：WorkManager 只保证"大概这个时间之后"，
 * 加一堆约束后可能晚十几分钟——而"20:00 提醒我打电话"晚一刻钟就是失职。
 * `setExactAndAllowWhileIdle` 能在 Doze 下按点唤醒。
 *
 * 没拿到精确闹钟权限（Android 12+ 的 `SCHEDULE_EXACT_ALARM`）时**降级**成
 * `setAndAllowWhileIdle`：可能晚几分钟，但绝不静默丢掉这个提醒——宁可迟到也不能不到。
 *
 * 登记策略是"整体重排"（见 [ReminderPlanner]）：任何日程变更后调一次 [sync]，
 * 把未来所有待办项重新登记一遍。幂等、漏不掉。
 */
class ReminderScheduler(
    private val context: Context,
    private val store: CalendarStore,
    private val dndStore: DndStore,
) : ReminderPlanner {

    private val alarmManager: AlarmManager? =
        context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager

    /**
     * 已登记的时刻表（itemId → 触发时刻）。用于取消那些已经不需要的闹钟：
     * AlarmManager 允许"同 requestCode 覆盖"，但**改期/取消之后旧的还得显式 cancel**，
     * 否则会按旧时间响一次。
     *
     * 注意它**只在内存里**：进程重启后是空的，所以重启前留下的旧闹钟不会被 cancel。
     * 那种情况靠两件事兜住：① 触发时 item 已被取消/完成 → 直接不投递；
     * ② 时刻对不上（见 [ReminderReceiver] 的陈旧闹钟校验）→ 重排而不投递。
     */
    private val registered = HashMap<Long, Long>()

    override suspend fun sync() {
        val manager = alarmManager ?: return
        val today = CalendarDates.todayText()
        val nowMs = System.currentTimeMillis()

        val wanted = LinkedHashMap<Long, Long>()
        store.upcoming(today).forEach { item ->
            if (item.time == null) return@forEach          // 全天项不参与到点提醒
            if (item.status != CalendarItemEntity.STATUS_TODO) return@forEach
            if (item.remindedAt != null) return@forEach
            val at = CalendarDates.toEpochMillis(item.date, item.time) ?: return@forEach
            if (at <= nowMs) return@forEach                // 已经过去的由补提醒路径处理
            wanted[item.id] = at
        }

        // 先撤掉不再需要的（取消、标完成、改期到过去、改了时刻）
        registered.filterKeys { it !in wanted.keys }.forEach { (id, _) -> cancel(manager, id) }
        registered.filter { (id, at) -> wanted[id] != null && wanted[id] != at }
            .forEach { (id, _) -> cancel(manager, id) }
        registered.clear()

        wanted.forEach { (id, at) ->
            if (schedule(manager, id, at)) registered[id] = at
        }
    }

    /** 直接给某一项排一个闹钟（补提醒 / 免打扰顺延时用）。 */
    fun scheduleAt(itemId: Long, atMillis: Long, deferred: Boolean = false): Boolean {
        val manager = alarmManager ?: return false
        val ok = schedule(manager, itemId, atMillis, deferred)
        if (ok) registered[itemId] = atMillis
        return ok
    }

    fun cancel(itemId: Long) {
        alarmManager?.let { cancel(it, itemId) }
        registered.remove(itemId)
    }

    private fun schedule(
        manager: AlarmManager,
        itemId: Long,
        atMillis: Long,
        deferred: Boolean = false,
    ): Boolean {
        val pending = pendingIntent(itemId, atMillis, deferred)
        return runCatching {
            val exact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || manager.canScheduleExactAlarms()
            if (exact) {
                manager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMillis, pending)
            } else {
                // 降级：不精确但绝不丢（见类注释）
                manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMillis, pending)
            }
            true
        }.getOrDefault(false)
    }

    private fun cancel(manager: AlarmManager, itemId: Long) {
        runCatching { manager.cancel(pendingIntent(itemId, 0L, false)) }
    }

    private fun pendingIntent(itemId: Long, atMillis: Long, deferred: Boolean): PendingIntent {
        val intent = Intent(context, ReminderReceiver::class.java).apply {
            action = ReminderReceiver.ACTION_FIRE
            putExtra(ReminderReceiver.EXTRA_ITEM_ID, itemId)
            // 把"当时排的是几点"一起带上：触发时如果和 item 现在的时刻对不上，
            // 说明这是个陈旧闹钟（进程重启后没被 cancel 掉的那种），要重排而不是投递
            putExtra(ReminderReceiver.EXTRA_SCHEDULED_AT, atMillis)
            putExtra(ReminderReceiver.EXTRA_DEFERRED, deferred)
            // data 唯一化 + requestCode 区分：不同项必须是不同的 PendingIntent，
            // 否则后一个会把前一个覆盖掉（extra 不参与 PendingIntent 的相等判断）
            data = android.net.Uri.parse("planner://reminder/$itemId")
        }
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        return PendingIntent.getBroadcast(context, itemId.toInt(), intent, flags)
    }

    /** 免打扰何时结束（用于把提醒顺延过去）。 */
    fun dndEndMillis(): Long {
        val settings = dndStore.load()
        settings.untilEpochMillis?.let { if (it > System.currentTimeMillis()) return it }
        val now = CalendarDates.now()
        if (!Dnd.inDnd(now, settings)) return 0L
        // 默认窗口跨天（22–8）：下一个 endHour 点就是结束时刻
        return Dnd.untilHour(now, Dnd.DEFAULT_END_HOUR)
    }
}
