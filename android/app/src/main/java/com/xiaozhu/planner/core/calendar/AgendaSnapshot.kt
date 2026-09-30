package com.xiaozhu.planner.core.calendar

import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.CalendarItemEntity.Companion.STATUS_TODO

/**
 * 议程快照：每轮注入 system prompt 的「今天 + 未来 7 天」。
 *
 * 为什么要有它：模型每轮都该知道"接下来有什么"，否则它会先调一次 `list_schedule`
 * 才敢说话——多一次往返、还容易漏。注入之后 `list_schedule` 只用于"要看别的范围"。
 *
 * 两个细节：
 * - **重复项折叠成一行**。不折的话"每天吃药"会把未来 7 天全占满，纯噪声。
 * - 只列前 [MAX_ROWS] 行，剩下的让它自己用 `list_schedule` 细看。
 */
object AgendaSnapshot {

    const val WINDOW_DAYS = 7L
    const val MAX_ROWS = 10

    /**
     * 生成注入文本。纯函数（只依赖传入的项与"现在"），方便单测。
     *
     * @param today 今天（yyyy-MM-dd）
     * @param items 今天起 7 天内**不含已取消**的日历项
     * @param hhmm 当前时刻（HH:mm），写进标题让人知道这份快照是什么时候的
     */
    fun render(
        today: String,
        items: List<CalendarItemEntity>,
        hhmm: String = CalendarDates.now().format(java.time.format.DateTimeFormatter.ofPattern("HH:mm")),
    ): String {
        val sb = StringBuilder()
        sb.append("[当前议程]（").append(today).append("，").append(hhmm).append("）\n")
        if (items.isEmpty()) {
            sb.append("（日历上未来 ").append(WINDOW_DAYS).append(" 天还是空的。）")
            return sb.toString()
        }

        val todo = items.filter { it.status == STATUS_TODO }
        sb.append("未来 ").append(WINDOW_DAYS).append(" 天共 ").append(items.size)
            .append(" 项，其中未完成 ").append(todo.size).append(" 项：\n")

        if (todo.isEmpty()) {
            sb.append("（全部已完成。）")
            return sb.toString()
        }

        // 重复项按系列折叠：每个系列只留最早那一期，并标出本窗口还剩几期
        val bySeries = LinkedHashMap<String, MutableList<CalendarItemEntity>>()
        val plain = mutableListOf<CalendarItemEntity>()
        todo.forEach { item ->
            val sid = item.seriesId
            if (sid.isNullOrBlank()) plain += item else bySeries.getOrPut(sid) { mutableListOf() } += item
        }

        val rows = (plain + bySeries.values.map { it.first() })
            .sortedWith(compareBy({ it.date }, { it.time ?: "" }))

        rows.take(MAX_ROWS).forEach { item ->
            sb.append("  #").append(item.id).append(' ').append(item.date).append(' ')
                .append(item.time ?: "全天").append(' ').append(item.content)
            val sid = item.seriesId
            if (!sid.isNullOrBlank()) {
                val label = REPEAT_LABELS[item.repeat] ?: "重复"
                val n = bySeries[sid]?.size ?: 1
                sb.append(if (n > 1) "（$label，本窗口还有 $n 期）" else "（$label）")
            }
            sb.append('\n')
        }
        if (rows.size > MAX_ROWS) {
            sb.append("  …另有 ").append(rows.size - MAX_ROWS).append(" 项（用 list_schedule 细看）")
        }
        return sb.toString().trimEnd()
    }

    val REPEAT_LABELS = mapOf("daily" to "每天", "weekly" to "每周", "weekdays" to "工作日")

    /**
     * 议程指纹：只取可变字段，变了才需要重新注入。
     *
     * 模型侧现在每轮重建 system prompt（比 PC 版更保守），所以这里主要给 UI/日志用；
     * 保留它是为了让"日程变了"这件事有个明确判断点，而不是靠时间戳。
     */
    fun fingerprint(items: List<CalendarItemEntity>): String =
        items.joinToString("|") { "${it.id},${it.date},${it.time},${it.status}" }
}
