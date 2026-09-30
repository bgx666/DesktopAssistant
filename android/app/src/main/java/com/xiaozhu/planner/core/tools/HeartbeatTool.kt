package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.double
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.calendar.CalendarDates
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/** 下一次「主动醒来」落在哪 —— 由 AlarmManager 真正唤醒（见 `proactive/HeartbeatScheduler`）。 */
interface HeartbeatStore {
    fun setNextWake(atMillis: Long, note: String)
    fun current(): Pair<Long, String>?
}

/**
 * 「把下次醒来真的排上闹钟」——工具层只依赖这个接口，实现是 AlarmManager。
 *
 * 为什么不让工具直接存时间就完事：PC 版的心跳由后端调度线程唤醒，安卓上没有那个线程，
 * 不排闹钟的话"心跳"就只是一个数字，永远等不到那一次醒来。
 */
interface HeartbeatPlanner {
    /** 按已存的醒来时间排闹钟（幂等：同一时刻重复排等于覆盖）。 */
    fun arm()
}

/**
 * heartbeat 工具：模型用它决定「下次什么时候自己醒」。
 *
 * 护栏（与 PC 版一致）：秒级最短 60 秒、分钟级最长 720 分钟。
 */
class HeartbeatTool(
    private val store: HeartbeatStore,
    private val planner: HeartbeatPlanner? = null,
    private val now: () -> ZonedDateTime = { ZonedDateTime.now() },
) : Tool {

    override val name = "heartbeat"

    override val description =
        "设定你下一次主动醒来的时间（不是「继续干活」的手段）。" +
            "有将来要做的事才设；纯问答不用设。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("seconds") { put("type", "number"); put("description", "多少秒后醒，最短 60") }
            putJsonObject("minutes") { put("type", "number"); put("description", "多少分钟后醒，最长 720") }
            putJsonObject("note") { put("type", "string"); put("description", "醒来时提醒自己干什么") }
        }
    }

    override suspend fun run(args: JsonObject): String {
        val seconds = args.double("seconds")
        val minutes = args.double("minutes")
        val note = args.str("note").orEmpty()

        val delayMinutes = when {
            seconds != null -> (seconds / 60.0).coerceAtLeast(MIN_MINUTES)
            minutes != null -> minutes.coerceIn(MIN_MINUTES, MAX_MINUTES)
            else -> return "要给 seconds 或 minutes 之一。"
        }
        val at = now().plusSeconds((delayMinutes * 60).toLong())
        store.setNextWake(at.toInstant().toEpochMilli(), note)
        // 真的排上闹钟：只存时间不排闹钟的话，这次"醒来"永远不会发生
        planner?.arm()

        val human = CalendarDates.fmtDuration(delayMinutes)
        val clock = at.format(DateTimeFormatter.ofPattern("HH:mm"))
        return "好，${human}后（$clock）我再来" + if (note.isBlank()) "。" else "，记着：$note。"
    }

    private companion object {
        const val MIN_MINUTES = 1.0
        const val MAX_MINUTES = 720.0
    }
}