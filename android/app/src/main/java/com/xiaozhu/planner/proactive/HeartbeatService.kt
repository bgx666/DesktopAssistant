package com.xiaozhu.planner.proactive

import android.app.Notification
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.xiaozhu.planner.R
import com.xiaozhu.planner.core.agent.AgentEvent
import com.xiaozhu.planner.core.agent.ConversationEngine
import com.xiaozhu.planner.core.agent.ProactivePrompts
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.reminder.ReminderNotifications
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * 后台跑一轮心跳：**它自己想一次，然后把话说出来**。
 *
 * 为什么必须是前台服务：安卓上后台进程随时会被冻住/杀掉，而这一轮要联网调模型、
 * 可能还要调工具（查日历、搜网页）——没有前台服务，跑到一半就断了，
 * 还会留下半截 buffer（assistant 声明了 tool_calls 却没有对应结果，下一轮直接 400）。
 *
 * 生命周期：起来 → 跑一轮 → 推通知 → **自己停掉**。不做常驻：
 * 常驻服务要一直挂通知、一直耗电，而心跳是"隔一阵醒一次"，不需要 24 小时在线。
 *
 * 超时保护（[ROUND_TIMEOUT_MS]）：模型卡住时不能让服务永远活着——
 * 前台服务的通知会一直挂在状态栏上，那比失败更糟。
 */
class HeartbeatService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var round: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val note = intent?.getStringExtra(EXTRA_NOTE).orEmpty()

        startForegroundCompat()

        round?.cancel()
        round = scope.launch {
            try {
                val reply = runHeartbeatRound(note)
                if (reply.isNullOrBlank()) {
                    ReminderNotifications.post(
                        context = this@HeartbeatService,
                        id = NOTIFICATION_ID,
                        lines = listOf("我醒了一下，没什么要说的。"),
                        title = "小助",
                    )
                } else {
                    ReminderNotifications.post(
                        context = this@HeartbeatService,
                        id = NOTIFICATION_ID,
                        lines = listOf(reply),
                        title = "小助",
                    )
                }
            } catch (_: Exception) {
                ReminderNotifications.post(
                    context = this@HeartbeatService,
                    id = NOTIFICATION_ID,
                    lines = listOf("我想说话，但这次没说出来（网络或服务出错了）。"),
                    title = "小助",
                )
            } finally {
                stopSelfSafely()
            }
        }

        return START_NOT_STICKY     // 被杀就别自动重来：那会变成"反复醒来"
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    /**
     * 跑一轮，返回要说的话（null = 没说/失败）。
     *
     * 用 `（系统：…）` 开头的消息喂进去：prompt 里已经写明这类消息是时间提示、
     * 不是用户在说话——与 PC 版心跳走的是同一条路径。
     */
    private suspend fun runHeartbeatRound(note: String): String? {
        val engine = ConversationEngine.get(application)
        if (!engine.settings.configured) return null

        val hhmm = CalendarDates.now().format(HHMM)
        val prompt = ProactivePrompts.heartbeat(hhmm, note, awayFor = awayFor(engine))

        val text = StringBuilder()
        val done = withTimeoutOrNull(ROUND_TIMEOUT_MS) {
            engine.runRound(ChatMessage.user(prompt)) { event ->
                if (event is AgentEvent.TextDelta) text.append(event.text)
            }
            true
        }
        // 超时也把已经生成的部分说出来：说了一半总比什么都不说强
        if (done == null && text.isBlank()) return null
        return text.toString().trim().ifBlank { null }
    }

    /** 距用户上次说话多久（prompt 要求用时间信息判断该不该开口）。 */
    private suspend fun awayFor(engine: ConversationEngine): String? = runCatching {
        val state = engine.conversation.loadState()
        val last = state.lastPlayerMessageAt
        if (last <= 0L) return@runCatching null
        val minutes = (System.currentTimeMillis() - last) / 60_000.0
        CalendarDates.fmtDuration(minutes)
    }.getOrNull()

    private fun startForegroundCompat() {
        ReminderNotifications.ensureChannel(this)
        val notification: Notification = NotificationCompat.Builder(this, ReminderNotifications.CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("小助在想事情")
            .setContentText("稍等，它想完就说。")
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setSilent(true)
            .build()

        runCatching {
            ServiceCompat.startForeground(
                this,
                FOREGROUND_ID,
                notification,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                } else {
                    0
                },
            )
        }
    }

    private fun stopSelfSafely() {
        runCatching { ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE) }
        stopSelf()
    }

    companion object {
        const val EXTRA_NOTE = "note"
        const val NOTIFICATION_ID = 90002
        private const val FOREGROUND_ID = 90003

        /** 一轮最多跑这么久（模型/网络卡住时别让前台服务挂着不走）。 */
        private const val ROUND_TIMEOUT_MS = 90_000L

        private val HHMM = java.time.format.DateTimeFormatter.ofPattern("HH:mm")

        fun intent(context: Context, note: String): Intent =
            Intent(context, HeartbeatService::class.java).putExtra(EXTRA_NOTE, note)
    }
}
