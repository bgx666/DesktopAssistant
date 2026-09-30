package com.xiaozhu.planner.reminder

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.xiaozhu.planner.MainActivity
import com.xiaozhu.planner.R

/**
 * 提醒通知：日历项到点时的**唯一**用户可见出口。
 *
 * 为什么提醒走通知而不是"让模型说话"：模型说话要联网、要前台、要 App 活着，
 * 而"晚上 8 点提醒我吃药"是用户明确排下的承诺——手机静音放着也得响。
 * 模型能说上话时（App 开着）会**额外**说一句（见 [ReminderEvents]），
 * 但通知不依赖它。
 */
object ReminderNotifications {

    const val CHANNEL_ID = "planner_reminder"
    const val CHANNEL_NAME = "日程提醒"

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            CHANNEL_NAME,
            NotificationManager.IMPORTANCE_HIGH,   // 到点提醒要能弹出来
        ).apply {
            description = "小助按日历时间提醒你"
            enableVibration(true)
        }
        manager.createNotificationChannel(channel)
    }

    fun canPost(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return true
        return ContextCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
    }

    /**
     * 弹一条提醒。[lines] 是"「内容」（原定 …）"形式的描述。
     *
     * 同一次触发里的多项合成一条通知：手机上连弹五条比一条都没有更烦。
     */
    fun post(context: Context, id: Int, lines: List<String>, late: Boolean = false) {
        if (lines.isEmpty()) return
        ensureChannel(context)
        if (!canPost(context)) return

        val title = when {
            late -> "有安排到点了（刚才在免打扰）"
            lines.size == 1 -> "该做这件事了"
            else -> "${lines.size} 件事到点了"
        }
        val text = lines.joinToString("\n")

        val tap = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pending = PendingIntent.getActivity(
            context,
            id,
            tap,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(lines.first())
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setAutoCancel(true)
            .setContentIntent(pending)
            .build()

        runCatching { NotificationManagerCompat.from(context).notify(id, notification) }
    }
}
