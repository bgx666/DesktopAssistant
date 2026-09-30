package com.xiaozhu.planner.data

import android.content.Context
import com.xiaozhu.planner.core.tools.HeartbeatStore

/** 下一次醒来时间落盘（P2 再交给 AlarmManager 真正唤醒）。 */
class PrefsHeartbeatStore(context: Context) : HeartbeatStore {

    private val prefs = context.getSharedPreferences("planner_state", Context.MODE_PRIVATE)

    override fun setNextWake(atMillis: Long, note: String) {
        prefs.edit().putLong(KEY_AT, atMillis).putString(KEY_NOTE, note).apply()
    }

    override fun current(): Pair<Long, String>? {
        val at = prefs.getLong(KEY_AT, 0L)
        if (at <= 0L) return null
        return at to prefs.getString(KEY_NOTE, "").orEmpty()
    }

    private companion object {
        const val KEY_AT = "next_wake_at"
        const val KEY_NOTE = "next_wake_note"
    }
}