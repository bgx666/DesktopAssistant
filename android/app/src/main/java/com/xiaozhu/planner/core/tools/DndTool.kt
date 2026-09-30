package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.bool
import com.xiaozhu.planner.core.agent.int
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.core.calendar.DndSettings
import com.xiaozhu.planner.core.calendar.DndStore
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import java.time.Instant
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/**
 * `set_do_not_disturb`：用户说"别吵我"时开、说"可以提醒我了"时关。
 *
 * 免打扰期间**不主动打扰**（提醒不弹、心跳不说话），但用户找它时照常回复——
 * 那是"不要主动打扰"，不是"闭嘴"。
 */
class SetDoNotDisturbTool(
    private val store: DndStore,
    private val now: () -> ZonedDateTime = { CalendarDates.now() },
) : Tool {

    override val name = "set_do_not_disturb"

    override val description =
        "开启或关闭免打扰。开启后这段时间内你不会主动打扰用户（用户找你仍然回复）。" +
            "用户说「别吵我/我在忙/睡觉了」时开，说「可以了/提醒我吧」时关。" +
            "不填 until_hour 就用默认夜间窗口 22:00–08:00。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("enabled") { put("type", "boolean"); put("description", "true＝开启，false＝关闭") }
            putJsonObject("until_hour") {
                put("type", "integer")
                put("description", "开到几点（0-23）。给了就不用默认窗口；已过的钟点顺延到明天")
            }
        }
        putJsonArray("required") { add("enabled") }
    }

    override suspend fun run(args: JsonObject): String {
        val enabled = args.bool("enabled", default = true)
        val rawHour = args.str("until_hour")
        val hour = if (rawHour == null) null else args.int("until_hour", -1).takeIf { it in 0..23 }
        if (rawHour != null && hour == null) {
            return "until_hour 应该是 0-23 的整数，收到 $rawHour"
        }

        return if (enabled) {
            val until = hour?.let { Dnd.untilHour(now(), it) }
            store.save(DndSettings(enabled = true, untilEpochMillis = until))
            val desc = if (until != null) {
                val at = Instant.ofEpochMilli(until).atZone(CalendarDates.ZONE)
                "直到 ${at.format(DateTimeFormatter.ofPattern("MM-dd HH:mm"))}"
            } else {
                "（默认 ${Dnd.DEFAULT_START_HOUR}:00-${Dnd.DEFAULT_END_HOUR}:00 窗口）"
            }
            "已开启免打扰$desc。期间我不会主动打扰你。"
        } else {
            store.save(DndSettings(enabled = false, untilEpochMillis = null))
            "已关闭免打扰，恢复正常提醒。"
        }
    }
}
