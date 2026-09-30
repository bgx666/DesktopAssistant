package com.xiaozhu.planner.proactive

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.tools.HeartbeatPlanner
import com.xiaozhu.planner.core.tools.HeartbeatStore

/**
 * 心跳闹钟：把"我下次什么时候醒"真的排进 AlarmManager。
 *
 * 与日历提醒的区别（prompt 里也写了，两边必须一致）：
 * - 日历提醒是**定点承诺**（20:00 提醒吃药）→ 精确闹钟，到点必响；
 * - 心跳是**自省节律**（隔多久自己醒一次看看）→ 不精确闹钟即可，
 *   Doze 里晚一点无所谓。用不精确闹钟还有个好处：**不占精确闹钟额度**，
 *   也不会因为 Android 12+ 没给 `SCHEDULE_EXACT_ALARM` 就完全失效。
 *
 * 心跳只有一个槽位（与 PC 版一致）：新的覆盖旧的——多件事靠日历，不靠心跳。
 */
class HeartbeatScheduler(
    private val context: Context,
    private val store: HeartbeatStore,
) : HeartbeatPlanner {

    private val alarmManager: AlarmManager? =
        context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager

    /** 按 [HeartbeatStore] 里记的下次醒来时间排闹钟（模型调完 heartbeat 后调它）。 */
    override fun arm() {
        val (at, _) = store.current() ?: return
        armAt(at)
    }

    fun armAt(atMillis: Long) {
        val manager = alarmManager ?: return
        if (atMillis <= System.currentTimeMillis()) return
        runCatching {
            // setAndAllowWhileIdle：Doze 下也能醒，但不保证精确——心跳要的就是这个语义
            manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMillis, pendingIntent())
        }
    }

    fun cancel() {
        runCatching { alarmManager?.cancel(pendingIntent()) }
    }

    /** 距下次醒来还有多久（毫秒）；没排返回 null。 */
    fun remainingMillis(): Long? {
        val (at, _) = store.current() ?: return null
        val left = at - System.currentTimeMillis()
        return if (left > 0) left else null
    }

    private fun pendingIntent(): PendingIntent {
        val intent = Intent(context, HeartbeatReceiver::class.java).apply {
            action = HeartbeatReceiver.ACTION_HEARTBEAT
            data = Uri.parse("planner://heartbeat")
        }
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        return PendingIntent.getBroadcast(context, HEARTBEAT_REQUEST, intent, flags)
    }

    companion object {
        private const val HEARTBEAT_REQUEST = 0x7A11

        /** 醒来时刻的人类可读形式（写进提示里让模型知道现在几点）。 */
        fun stamp(atMillis: Long): String =
            java.time.Instant.ofEpochMilli(atMillis)
                .atZone(CalendarDates.ZONE)
                .format(java.time.format.DateTimeFormatter.ofPattern("HH:mm"))
    }
}
