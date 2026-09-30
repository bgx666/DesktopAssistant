package com.xiaozhu.planner.data

import android.content.Context
import com.xiaozhu.planner.core.calendar.DndSettings
import com.xiaozhu.planner.core.calendar.DndStore

/** 免打扰状态落盘（普通 Prefs：不是秘密，也不该塞进加密存储）。 */
class PrefsDndStore(context: Context) : DndStore {

    private val prefs = context.getSharedPreferences("planner_state", Context.MODE_PRIVATE)

    override fun load(): DndSettings = DndSettings(
        enabled = prefs.getBoolean(KEY_ENABLED, true),
        untilEpochMillis = prefs.getLong(KEY_UNTIL, 0L).takeIf { it > 0L },
    )

    override fun save(settings: DndSettings) {
        prefs.edit()
            .putBoolean(KEY_ENABLED, settings.enabled)
            .putLong(KEY_UNTIL, settings.untilEpochMillis ?: 0L)
            .apply()
    }

    private companion object {
        const val KEY_ENABLED = "dnd_enabled"
        const val KEY_UNTIL = "dnd_until"
    }
}
