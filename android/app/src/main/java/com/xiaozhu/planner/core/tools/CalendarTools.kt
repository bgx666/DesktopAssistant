package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.bool
import com.xiaozhu.planner.core.agent.long
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.calendar.AgendaSnapshot
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.ReminderPlanner
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.KIND_EVENT
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.KINDS
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.REPEAT_RULES
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_CANCELLED
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_DONE
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_TODO
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * 日历四件套：`list_schedule` / `add_schedule` / `update_schedule` / `cancel_schedule`。
 *
 * 这是**唯一**的待办概念（PC 版已经把"任务/推进队列"整套删掉了，这里不要再引入）。
 *
 * 关键约定（prompt 里也写了，两边必须一致）：
 * - 用户给了明确时刻 → 写 `HH:MM` 进 `time`，**由系统的精确闹钟在那一刻通知**；
 *   不要折算成心跳分钟数（心跳是单槽位，多件事互相覆盖，也到不了分钟精度）。
 * - 重复（每天/每周/工作日）→ 用 `repeat`，不要一条条加。
 * - 重复项默认"只改/只取消这一次"，用户说"以后都不"才 `whole_series = true`。
 *
 * 每个写操作之后都调一次 [ReminderPlanner.sync]——闹钟重排是幂等的整体重排，
 * 比"逐路径注册/注销"少一类漏改的 bug。
 */
class ListScheduleTool(private val store: CalendarStore) : Tool {

    override val name = "list_schedule"

    override val description =
        "查看某个日期范围内的日程与待办（含时刻、状态、id）。" +
            "问「接下来几天有什么安排」「这周还有什么没做」时用。闭区间；" +
            "end_date 留空就只看 start_date 当天。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("start_date") { put("type", "string"); put("description", "起始日期 YYYY-MM-DD") }
            putJsonObject("end_date") { put("type", "string"); put("description", "结束日期 YYYY-MM-DD（留空＝只看当天）") }
        }
        putJsonArray("required") { add("start_date") }
    }

    override suspend fun run(args: JsonObject): String {
        val rawStart = args.str("start_date")
        val start = CalendarDates.normDate(rawStart)
            ?: return "日期格式应为 YYYY-MM-DD，收到 $rawStart"
        val rawEnd = args.str("end_date")
        var end = if (rawEnd.isNullOrBlank()) start
        else CalendarDates.normDate(rawEnd) ?: return "日期格式应为 YYYY-MM-DD，收到 $rawEnd"
        if (end < start) end = start   // 传反了就按同一天处理，不报错
        return renderRange(start, end, store.range(start, end))
    }

    private fun renderRange(start: String, end: String, items: List<CalendarItemEntity>): String {
        if (items.isEmpty()) return "$start ~ $end 没有任何日程或待办。"
        val lines = mutableListOf("$start ~ $end 共 ${items.size} 项：")
        items.forEach { item ->
            val mark = when (item.status) {
                STATUS_DONE -> "[已完成]"
                STATUS_CANCELLED -> "[已取消]"
                else -> ""
            }
            val kind = if (item.kind == KIND_EVENT) "日程" else "任务"
            val detail = if (item.detail.isNotBlank()) "（${item.detail}）" else ""
            // 重复项标出来：模型据此判断"取消/改"是指这一次还是整个系列
            val rep = item.seriesId?.takeIf { it.isNotBlank() }?.let {
                " ↻" + (AgendaSnapshot.REPEAT_LABELS[item.repeat] ?: "重复")
            }.orEmpty()
            lines += "  #${item.id} ${item.date} ${item.time ?: "全天"} [$kind]$mark$rep ${item.content}$detail"
        }
        return lines.joinToString("\n")
    }
}

class AddScheduleTool(
    private val store: CalendarStore,
    private val planner: ReminderPlanner,
) : Tool {

    override val name = "add_schedule"

    override val description =
        "往日历加一条日程或待办。用户说「记一下/提醒我/安排/加到日历」时用。" +
            "给了明确时刻就填 time（HH:MM，如 20:00）——到点手机会自动弹提醒；" +
            "只是当天要做、没指定几点就留空（全天项）。" +
            "要重复的用 repeat（daily 每天 / weekly 每周同一天 / weekdays 工作日），别一条条加。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("date") { put("type", "string"); put("description", "日期 YYYY-MM-DD（重复项＝起始日）") }
            putJsonObject("content") { put("type", "string"); put("description", "做什么") }
            putJsonObject("time") { put("type", "string"); put("description", "时刻 HH:MM，留空＝全天") }
            putJsonObject("kind") { put("type", "string"); put("description", "event（日程）/ task（当天任务）") }
            putJsonObject("detail") { put("type", "string"); put("description", "备注（可选）") }
            putJsonObject("repeat") { put("type", "string"); put("description", "留空＝一次性；daily / weekly / weekdays") }
        }
        putJsonArray("required") { add("date"); add("content") }
    }

    override suspend fun run(args: JsonObject): String {
        val rawDate = args.str("date")
        val date = CalendarDates.normDate(rawDate)
            ?: return "日期格式应为 YYYY-MM-DD，收到 $rawDate"
        val content = args.str("content")?.trim().orEmpty()
        if (content.isEmpty()) return "内容不能为空。"

        val rawTime = args.str("time")
        val time = if (rawTime.isNullOrBlank()) null
        else CalendarDates.normTime(rawTime)
            ?: return "时刻格式应为 HH:MM（如 20:00），收到 $rawTime"

        val kind = args.str("kind")?.trim().orEmpty().ifBlank { KIND_EVENT }
        if (kind !in KINDS) return "kind 必须是 event/task，收到 $kind"

        val rule = args.str("repeat")?.trim()?.lowercase().orEmpty()
        if (rule.isNotEmpty() && rule !in REPEAT_RULES) {
            return "repeat 只能是 daily/weekly/weekdays（或留空），收到 $rule"
        }
        val detail = args.str("detail").orEmpty()

        if (rule.isNotEmpty()) {
            val (sid, count) = store.addSeries(date, content, rule, time, kind, detail)
            if (sid.isEmpty()) return "重复规则没有算出任何日期，请检查起始日。"
            planner.sync()
            val label = when (rule) {
                "daily" -> "每天"
                "weekly" -> "每周同一天"
                else -> "工作日"
            }
            return "已加入日历（$label，从 $date 起，共排了 $count 期，之后会自动续）：" +
                "${time ?: "（全天）"}「$content」" +
                if (time != null) "，到点手机会弹提醒。" else "。"
        }

        val id = store.add(date, content, time, kind, detail)
        planner.sync()
        return "已加入日历 #$id：$date ${time ?: "（全天）"}「$content」" +
            if (time != null) "，到点手机会弹提醒。" else "。"
    }
}

class UpdateScheduleTool(
    private val store: CalendarStore,
    private val planner: ReminderPlanner,
) : Tool {

    override val name = "update_schedule"

    override val description =
        "修改日历里的一项：改期 / 改时刻 / 改内容 / 标完成或撤销完成。只传要改的字段。" +
            "取消请用 cancel_schedule。这一项属于重复系列时（list_schedule 会标出来）：" +
            "默认只改这一次；要改今后所有还没做的（改时刻/内容）传 whole_series=true" +
            "（改日期和标完成永远只作用于这一项，那是历史不是规则）。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("item_id") { put("type", "integer"); put("description", "项目 id（list_schedule 返回的 #id）") }
            putJsonObject("date") { put("type", "string"); put("description", "新日期 YYYY-MM-DD（不改留空）") }
            putJsonObject("time") { put("type", "string"); put("description", "新时刻 HH:MM；传「全天」表示去掉时刻") }
            putJsonObject("content") { put("type", "string"); put("description", "新内容（不改留空）") }
            putJsonObject("detail") { put("type", "string"); put("description", "新备注（不改留空）") }
            putJsonObject("status") { put("type", "string"); put("description", "todo（未完成）/ done（已完成）") }
            putJsonObject("whole_series") { put("type", "boolean"); put("description", "true＝应用到该系列今后所有未完成的项") }
        }
        putJsonArray("required") { add("item_id") }
    }

    override suspend fun run(args: JsonObject): String {
        val id = args.long("item_id", 0L)
        if (id <= 0) return "要给 item_id（list_schedule 返回的 #id）。"
        val item = store.byId(id) ?: return "找不到日历项 #$id。"

        val date = args.str("date")?.let {
            CalendarDates.normDate(it) ?: return "日期格式应为 YYYY-MM-DD，收到 $it"
        }
        val rawTime = args.str("time")
        val clearTime = rawTime != null && rawTime.trim() in listOf("全天", "all-day", "-")
        val time = if (rawTime == null || clearTime) null
        else CalendarDates.normTime(rawTime)
            ?: return "时刻格式应为 HH:MM（如 20:00），收到 $rawTime"
        val content = args.str("content")
        val detail = args.str("detail")
        val status = args.str("status")?.trim()
        if (status != null && status !in listOf(STATUS_TODO, STATUS_DONE)) {
            return "status 必须是 todo/done，收到 $status（取消请用 cancel_schedule）"
        }
        val wholeSeries = args.bool("whole_series")

        if (date == null && time == null && !clearTime && content == null && detail == null && status == null) {
            return "没有要修改的字段。"
        }

        // 整个系列：只有"规则型字段"能批量改；日期/完成状态是这一次的事
        if (wholeSeries) {
            val sid = item.seriesId
            if (sid.isNullOrBlank()) return "#$id 不是重复项，whole_series 无意义（已按单次处理）。"
            if (date != null || status != null) {
                return "#$id 属于重复系列，但 date/status 只能改这一次" +
                    "（要改期或标完成，请对具体那一条操作）。"
            }
            val n = store.updateSeries(
                sid = sid,
                fromDate = item.date,
                time = if (clearTime) null else (time ?: item.time),
                kind = item.kind,
                content = content?.trim()?.takeIf { it.isNotEmpty() } ?: item.content,
                detail = detail?.trim()?.takeIf { it.isNotEmpty() } ?: item.detail,
            )
            planner.sync()
            return "已更新重复系列（${item.date} 起、未完成的 $n 项）。"
        }

        val ok = store.update(
            id = id,
            date = date,
            time = time,
            clearTime = clearTime,
            content = content,
            detail = detail,
            status = status,
        )
        if (!ok) return "修改 #$id 失败。"
        planner.sync()
        val changed = buildList {
            date?.let { add("日期") }
            if (clearTime) add("时刻（改为全天）") else time?.let { add("时刻") }
            content?.let { add("内容") }
            detail?.let { add("备注") }
            status?.let { add("状态") }
        }
        return "已更新日历项 #$id（${changed.joinToString("、")}）。"
    }
}

class CancelScheduleTool(
    private val store: CalendarStore,
    private val planner: ReminderPlanner,
) : Tool {

    override val name = "cancel_schedule"

    override val description =
        "取消日历里的一项（用户说「不去了/取消掉/这个不做了」时用）。软取消：保留痕迹，" +
            "不再出现在日历与提醒里。这一项属于重复系列时默认只取消这一次；" +
            "用户说「以后都不去了/别再排了」→ whole_series=true。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("item_id") { put("type", "integer"); put("description", "项目 id（list_schedule 返回的 #id）") }
            putJsonObject("whole_series") { put("type", "boolean"); put("description", "true＝连同该系列今后所有未完成的项一起取消") }
        }
        putJsonArray("required") { add("item_id") }
    }

    override suspend fun run(args: JsonObject): String {
        val id = args.long("item_id", 0L)
        if (id <= 0) return "要给 item_id（list_schedule 返回的 #id）。"
        val item = store.byId(id) ?: return "找不到日历项 #$id。"
        val wholeSeries = args.bool("whole_series")

        val sid = item.seriesId
        if (wholeSeries && !sid.isNullOrBlank()) {
            val n = store.cancelSeries(sid, item.date)
            planner.sync()
            return "已取消重复项「${item.content}」自 ${item.date} 起、今后未完成的 $n 期；之前已完成的不动。"
        }
        if (!store.cancel(id)) return "取消 #$id 失败。"
        planner.sync()
        val tail = if (!sid.isNullOrBlank()) {
            "（它属于重复系列，今后各期仍在——要全取消请传 whole_series。）"
        } else ""
        return "已取消 #$id「${item.content}」（${item.date} ${item.time ?: "全天"}）。$tail"
    }
}
