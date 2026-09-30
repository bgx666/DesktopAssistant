package com.xiaozhu.planner.core.calendar

import java.time.ZonedDateTime

/**
 * 免打扰：显式开关 + 可选的"直到几点"，否则用默认夜间窗口（22:00–08:00）。
 *
 * 纯函数实现，语义与 PC 版 `session.py::in_dnd` 一致（含跨天窗口）。
 */
data class DndSettings(
    val enabled: Boolean = true,
    /** 显式打开时给的截止时刻（epoch 毫秒）；到点后自动恢复。 */
    val untilEpochMillis: Long? = null,
)

object Dnd {

    const val DEFAULT_START_HOUR = 22
    const val DEFAULT_END_HOUR = 8

    /**
     * 此刻是否处于免打扰。
     *
     * 注意 `untilEpochMillis` 的优先级**高于** `enabled`：显式开了"直到 8 点"之后，
     * 到点就该自动结束，而不是继续套默认窗口。
     */
    fun inDnd(
        now: ZonedDateTime,
        settings: DndSettings,
        startHour: Int = DEFAULT_START_HOUR,
        endHour: Int = DEFAULT_END_HOUR,
    ): Boolean {
        settings.untilEpochMillis?.let { until ->
            if (now.toInstant().toEpochMilli() < until) return true
        }
        if (!settings.enabled) return false
        val h = now.hour
        return if (startHour <= endHour) {
            h in startHour until endHour
        } else {
            h >= startHour || h < endHour   // 跨天窗口（22–8）
        }
    }

    /** 解析"直到几点"：给的钟点已过就顺延到明天（跟 PC 版一致）。 */
    fun untilHour(now: ZonedDateTime, hour: Int): Long {
        var target = now.withHour(hour.coerceIn(0, 23)).withMinute(0).withSecond(0).withNano(0)
        if (!target.isAfter(now)) target = target.plusDays(1)
        return target.toInstant().toEpochMilli()
    }
}

/** 免打扰状态的持久化（实现放 data 层）。 */
interface DndStore {
    fun load(): DndSettings
    fun save(settings: DndSettings)
}
