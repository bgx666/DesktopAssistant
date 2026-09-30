package com.xiaozhu.planner.core.calendar

import com.xiaozhu.planner.data.db.CalendarDao
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.KIND_EVENT
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.KINDS
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.REPEAT_RULES
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_CANCELLED
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_DONE
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_TODO
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUSES
import com.xiaozhu.planner.data.db.SeriesRow
import java.time.LocalDate
import java.util.UUID

/**
 * 日历的语义层（复刻 PC 版 `store/calendar_db.py` 的全部行为）。
 *
 * 三个关键决定，都是从 PC 版的踩坑记录里带过来的：
 *
 * 1. **重复走"生成实体行"而不是查询时虚拟展开**。到点提醒靠"真实的行 + remindedAt 防重"，
 *    而"这一次提醒过了/做过了/改期了"终究要落到行上——那就干脆生成行，勾完成、取消单次、
 *    改期全部复用同一套代码。代价是表达不了"无限重复"：只能生成到 horizon，
 *    由 [extendSeries] 滚动续期兜住（否则一年后"每天吃药"会默默消失）。
 * 2. **取消是软取消**（status = cancelled）：保留痕迹，日历页还能看见"这条不做了"。
 * 3. **全天项（time = null）不参与到点提醒**——由当天的议程快照覆盖，
 *    否则"今天复习线代"会在 00:00 半夜响一声。
 */
class CalendarStore(private val dao: CalendarDao) {

    // ── 写入 ────────────────────────────────────────────────

    suspend fun add(
        date: String,
        content: String,
        time: String? = null,
        kind: String = KIND_EVENT,
        detail: String = "",
    ): Long = dao.insert(
        CalendarItemEntity(
            date = CalendarDates.normDate(date) ?: date,
            time = time?.let { CalendarDates.normTime(it) },
            kind = kind.takeIf { it in KINDS } ?: KIND_EVENT,
            content = content.trim(),
            detail = detail,
        )
    )

    /**
     * 生成一整串重复项，返回 (seriesId, 生成条数)。
     *
     * @param horizonDays 生成到起始日 + horizon（默认 1 年），之后由 [extendSeries] 补。
     */
    suspend fun addSeries(
        date: String,
        content: String,
        rule: String,
        time: String? = null,
        kind: String = KIND_EVENT,
        detail: String = "",
        horizonDays: Long = REPEAT_HORIZON_DAYS,
    ): Pair<String, Int> {
        require(rule in REPEAT_RULES) { "不支持的重复规则: $rule" }
        val start = CalendarDates.normDate(date) ?: return "" to 0
        val until = CalendarDates.plusDays(start, horizonDays) ?: return "" to 0
        val dates = CalendarDates.seriesDates(rule, start, until, start)
        if (dates.isEmpty()) return "" to 0

        val sid = UUID.randomUUID().toString().replace("-", "").take(12)
        val t = time?.let { CalendarDates.normTime(it) }
        val k = kind.takeIf { it in KINDS } ?: KIND_EVENT
        val text = content.trim()
        dao.insertAll(
            dates.map { d ->
                CalendarItemEntity(
                    date = d,
                    time = t,
                    kind = k,
                    content = text,
                    detail = detail,
                    seriesId = sid,
                    repeat = rule,
                    repeatUntil = until,
                )
            }
        )
        return sid to dates.size
    }

    /** 改一项（白名单字段）；返回是否真的改到。 */
    suspend fun update(
        id: Long,
        date: String? = null,
        time: String? = null,
        clearTime: Boolean = false,
        content: String? = null,
        detail: String? = null,
        kind: String? = null,
        status: String? = null,
    ): Boolean {
        val item = dao.byId(id) ?: return false
        var touched = false
        date?.let { CalendarDates.normDate(it) }?.let { dao.setDate(id, it); touched = true }
        if (clearTime) {
            dao.setTime(id, null); touched = true
        } else {
            time?.let { CalendarDates.normTime(it) }?.let { dao.setTime(id, it); touched = true }
        }
        content?.takeIf { it.isNotBlank() }?.let { dao.setContent(id, it.trim()); touched = true }
        detail?.takeIf { it.isNotBlank() }?.let { dao.setDetail(id, it.trim()); touched = true }
        kind?.takeIf { it in KINDS }?.let { dao.setKind(id, it); touched = true }
        status?.takeIf { it in STATUSES }?.let { setStatus(id, it); touched = true }
        // 改了时间/日期 → 旧的一次性闹钟作废、remindedAt 也要清（否则改到未来却不再提醒）
        if (touched && item.status == STATUS_TODO) {
            val changedSchedule = date != null || time != null || clearTime
            if (changedSchedule) dao.clearReminded(id)
        }
        return touched
    }

    suspend fun setStatus(id: Long, status: String): Boolean {
        if (status !in STATUSES) return false
        val doneAt = if (status == STATUS_DONE) System.currentTimeMillis() else null
        return dao.setStatus(id, status, doneAt) > 0
    }

    suspend fun cancel(id: Long): Boolean = setStatus(id, STATUS_CANCELLED)

    /** 批量改系列里"fromDate 起、未完成"的行（改内容/时刻/类型，不动已完成的历史）。 */
    suspend fun updateSeries(
        sid: String,
        fromDate: String,
        time: String?,
        kind: String,
        content: String,
        detail: String,
    ): Int = dao.updateSeries(sid, fromDate, time, kind, content, detail)

    /** 取消系列里"fromDate 起、未完成"的行，返回行数。 */
    suspend fun cancelSeries(sid: String, fromDate: String): Int = dao.cancelSeries(sid, fromDate)

    // ── 读取 ────────────────────────────────────────────────

    suspend fun byId(id: Long): CalendarItemEntity? = dao.byId(id)

    /** 闭区间查询（不含已取消）。 */
    suspend fun range(start: String, end: String): List<CalendarItemEntity> = dao.range(start, end)

    /** 闭区间查询（含已取消，日历页用）。 */
    suspend fun rangeAll(start: String, end: String): List<CalendarItemEntity> = dao.rangeAll(start, end)

    suspend fun day(date: String): List<CalendarItemEntity> = dao.day(date)

    suspend fun seriesSummary(): List<SeriesRow> = dao.seriesSummary()

    suspend fun seriesItems(sid: String): List<CalendarItemEntity> = dao.seriesItems(sid)

    // ── 滚动续期 ────────────────────────────────────────────

    suspend fun seriesNeedExtend(today: String = CalendarDates.todayText()): List<String> {
        val cutoff = CalendarDates.plusDays(today, REPEAT_EXTEND_WHEN_LEFT) ?: today
        return dao.seriesNeedExtend(cutoff)
    }

    /**
     * 把系列续到 `today + horizonDays`，返回新增条数。
     *
     * [today] 可显式传：续期是"未来才发生"的行为，没有注入点就没法测。
     */
    suspend fun extendSeries(
        sid: String,
        today: String = CalendarDates.todayText(),
        horizonDays: Long = REPEAT_HORIZON_DAYS,
    ): Int {
        val items = dao.seriesItems(sid)
        val first = items.firstOrNull() ?: return 0
        val rule = first.repeat ?: return 0
        val target = CalendarDates.plusDays(today, horizonDays) ?: return 0
        val last = items.mapNotNull { it.repeatUntil ?: it.date }.maxOrNull() ?: first.date
        if (target <= last) return 0
        val start = LocalDate.parse(last).plusDays(1).let { CalendarDates.dayText(it) }
        // base 仍用最初起始日，保证 weekly 不漂移
        val dates = CalendarDates.seriesDates(rule, start, target, first.date)
        if (dates.isEmpty()) return 0
        dao.insertAll(
            dates.map { d ->
                first.copy(
                    id = 0,
                    date = d,
                    seriesId = sid,
                    repeat = rule,
                    repeatUntil = target,
                    status = STATUS_TODO,
                    remindedAt = null,
                    doneAt = null,
                    createdAt = System.currentTimeMillis(),
                )
            }
        )
        return dates.size
    }

    /** 一次把所有该续的系列都续掉（App 启动 / 每天一次）。返回续期条数合计。 */
    suspend fun extendAllSeries(today: String = CalendarDates.todayText()): Int {
        var total = 0
        seriesNeedExtend(today).forEach { sid -> total += extendSeries(sid, today) }
        return total
    }

    // ── 到点提醒 ────────────────────────────────────────────

    /** 到点且尚未提醒过的待办项。 */
    suspend fun due(stamp: String = CalendarDates.stamp()): List<CalendarItemEntity> = dao.due(stamp)

    suspend fun markReminded(id: Long, at: String = CalendarDates.stamp()) {
        dao.markReminded(id, at)
    }

    /** 已经过期但还没提醒过的（跨重启补提醒）。 */
    suspend fun overdue(stamp: String = CalendarDates.stamp()): List<CalendarItemEntity> =
        dao.overdue(stamp)

    /** 未来的待办项（重排闹钟）。 */
    suspend fun upcoming(today: String = CalendarDates.todayText()): List<CalendarItemEntity> =
        dao.upcoming(today)

    companion object {
        const val REPEAT_HORIZON_DAYS = 365L
        const val REPEAT_EXTEND_WHEN_LEFT = 60L
    }
}
