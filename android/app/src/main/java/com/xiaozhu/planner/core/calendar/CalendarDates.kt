package com.xiaozhu.planner.core.calendar

import java.time.DayOfWeek
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/**
 * 日历用的日期/时刻工具：**纯函数**，不碰 Android API，所以能直接单测。
 *
 * 时区固定 UTC+8（跟 PC 版一致，模型也被告知"北京时间"）。所有日期都是
 * `yyyy-MM-dd`、时刻都是 `HH:mm` 字符串——与 PC 版 `store/calendar_db.py` 对齐。
 */
object CalendarDates {

    val ZONE: ZoneId = ZoneId.of("Asia/Shanghai")
    private val DATE_RE = Regex("""\d{4}-\d{2}-\d{2}""")
    private val TIME_RE = Regex("""(\d{1,2}):(\d{2})""")
    private val DAY = DateTimeFormatter.ofPattern("yyyy-MM-dd")
    private val MINUTE = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm")

    fun now(): ZonedDateTime = ZonedDateTime.now(ZONE)

    fun today(): LocalDate = now().toLocalDate()

    fun todayText(): String = today().format(DAY)

    fun dayText(d: LocalDate): String = d.format(DAY)

    /** `yyyy-MM-dd HH:mm`（到点判据与 remindedAt 都用这个格式）。 */
    fun stamp(at: ZonedDateTime = now()): String = at.format(MINUTE)

    /** 归一化日期；非法返回 null（不接受"明天"这类相对说法，那是模型该算好的）。 */
    fun normDate(raw: String?): String? {
        val s = raw?.trim().orEmpty()
        if (!DATE_RE.matches(s)) return null
        return runCatching { LocalDate.parse(s).format(DAY) }.getOrNull()
    }

    /** 归一化时刻：接受 `9:05` / `09:05`；非法返回 null。 */
    fun normTime(raw: String?): String? {
        val s = raw?.trim().orEmpty()
        val m = TIME_RE.matchEntire(s) ?: return null
        val h = m.groupValues[1].toInt()
        val mi = m.groupValues[2].toInt()
        if (h > 23 || mi > 59) return null
        return "%02d:%02d".format(h, mi)
    }

    /** 日历项排序/比较用的键：全天项（time = null）视为当天 00:00。 */
    fun sortKey(date: String, time: String?): String = "$date ${time ?: "00:00"}"

    /** 到点判据用的键：全天项视为当天 23:59（跟 PC 版 `list_overdue_calendar` 一致）。 */
    fun dueKey(date: String, time: String?): String = "$date ${time ?: "23:59"}"

    /**
     * 规则命中的日期列表（闭区间 [start, until]）。
     *
     * [baseDate] 决定 weekly 是"星期几"——续期时必须仍传最初的起始日，
     * 否则用续期起点当基准会漂到别的星期几（PC 版踩过这个坑，注释留在那里）。
     */
    fun seriesDates(rule: String, start: String, until: String, baseDate: String): List<String> {
        val d0 = normDate(start) ?: return emptyList()
        val d1 = normDate(until) ?: return emptyList()
        val base = normDate(baseDate) ?: return emptyList()
        var cur = LocalDate.parse(d0)
        val end = LocalDate.parse(d1)
        if (end < cur) return emptyList()
        val baseWeekday: DayOfWeek = LocalDate.parse(base).dayOfWeek
        val out = mutableListOf<String>()
        while (cur <= end) {
            val hit = when (rule) {
                "daily" -> true
                "weekly" -> cur.dayOfWeek == baseWeekday
                "weekdays" -> cur.dayOfWeek.value <= 5
                else -> false
            }
            if (hit) out += cur.format(DAY)
            cur = cur.plusDays(1)
        }
        return out
    }

    fun plusDays(date: String, days: Long): String? =
        normDate(date)?.let { LocalDate.parse(it).plusDays(days).format(DAY) }

    /** `yyyy-MM-dd HH:mm` → epoch 毫秒（闹钟用）；格式不对返回 null。 */
    fun toEpochMillis(date: String, time: String?): Long? {
        val d = normDate(date) ?: return null
        val t = normTime(time ?: return null) ?: return null
        return runCatching {
            LocalDateTime.parse("$d $t", DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm"))
                .atZone(ZONE).toInstant().toEpochMilli()
        }.getOrNull()
    }

    /** 人类可读的时长（心跳提示里用，PC 版 `_fmt_duration` 的近似复刻）。 */
    fun fmtDuration(minutes: Double): String = when {
        minutes < 1 -> "${(minutes * 60).toInt()} 秒"
        minutes < 60 -> "${minutes.toInt()} 分钟"
        else -> {
            val h = minutes / 60
            if (h == h.toInt().toDouble()) "${h.toInt()} 小时" else "%.1f 小时".format(h)
        }
    }
}
