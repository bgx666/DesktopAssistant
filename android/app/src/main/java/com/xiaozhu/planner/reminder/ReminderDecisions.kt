package com.xiaozhu.planner.reminder

import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_TODO

/**
 * 闹钟响了之后该怎么办 —— **纯函数**，不碰 AlarmManager / 通知 / 数据库。
 *
 * 单独抽出来是因为这里的判断全是"差一点就错"的边界：
 * 陈旧闹钟、免打扰顺延、已完成/已取消、时刻被改到过去……埋在 Receiver 里
 * 就只能靠真机手测，而每一条错了都表现为"提醒没响"或"提醒响错时间"，很难复盘。
 */
sealed interface ReminderDecision {
    /** 什么都不做（项已不存在 / 已完成 / 已取消）。 */
    object Skip : ReminderDecision

    /** 投递：[line] 是要展示的描述。 */
    data class Deliver(val line: String) : ReminderDecision

    /** 改到 [atMillis] 再响（陈旧闹钟按新时刻重排；免打扰顺延到窗口结束）。 */
    data class Reschedule(val atMillis: Long, val deferred: Boolean) : ReminderDecision
}

object ReminderDecisions {

    fun decide(
        item: CalendarItemEntity?,
        scheduledAt: Long,
        deferred: Boolean,
        inDnd: Boolean,
        dndEndMillis: Long,
        nowMillis: Long,
    ): ReminderDecision {
        if (item == null) return ReminderDecision.Skip
        if (item.status != STATUS_TODO) return ReminderDecision.Skip

        // 陈旧闹钟：进程重启后调度器记不住旧闹钟，改期前的那一个会在**旧时刻**触发。
        // 不校验的话会"20:00 先响一次并标上已提醒，21:00 反而不再提醒"——比不提醒更糟。
        val expected = CalendarDates.toEpochMillis(item.date, item.time)
        if (!deferred && scheduledAt > 0L && expected != null && scheduledAt != expected) {
            return if (expected > nowMillis) {
                ReminderDecision.Reschedule(expected, deferred = false)
            } else {
                // 改到的时间也过去了：交给补提醒路径，别在这里重复投递
                ReminderDecision.Skip
            }
        }

        if (inDnd) {
            return if (dndEndMillis > nowMillis) {
                ReminderDecision.Reschedule(dndEndMillis, deferred = true)
            } else {
                ReminderDecision.Skip
            }
        }

        return ReminderDecision.Deliver("「${item.content}」（原定 ${item.date} ${item.time ?: "全天"}）")
    }
}
