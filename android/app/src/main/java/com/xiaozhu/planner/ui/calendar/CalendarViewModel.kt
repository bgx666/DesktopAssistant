package com.xiaozhu.planner.ui.calendar

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.core.calendar.DndSettings
import com.xiaozhu.planner.core.calendar.DndStore
import com.xiaozhu.planner.data.PrefsDndStore
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import com.xiaozhu.planner.data.db.SeriesRow
import com.xiaozhu.planner.reminder.ReminderScheduler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class CalendarUiState(
    val date: String = CalendarDates.todayText(),
    val items: List<CalendarItemEntity> = emptyList(),
    val series: List<SeriesRow> = emptyList(),
    val dnd: DndSettings = DndSettings(),
    val inDnd: Boolean = false,
    val status: String = "",
)

/**
 * 日历页：用户自己看/自己改日程（这是产品的前提——日程不是"模型的黑箱"，
 * 用户能直接在手机上加、改、勾完成，模型也读写同一份数据）。
 *
 * 与 ChatViewModel 共用同一个数据库单例，但各建一份轻量包装（Room 的 DAO 是单例的）。
 */
class CalendarViewModel(app: Application) : AndroidViewModel(app) {

    private val db = PlannerDatabase.get(app)
    private val store = CalendarStore(db.calendar())
    private val dndStore: DndStore = PrefsDndStore(app)
    private val scheduler = ReminderScheduler(app, store, dndStore)

    private val _state = MutableStateFlow(CalendarUiState())
    val state: StateFlow<CalendarUiState> = _state.asStateFlow()

    init {
        reload()
    }

    fun reload() {
        viewModelScope.launch {
            val date = _state.value.date
            withContext(Dispatchers.IO) {
                val items = store.rangeAll(date, date)
                val series = store.seriesSummary()
                val dnd = dndStore.load()
                _state.update {
                    it.copy(
                        items = items,
                        series = series,
                        dnd = dnd,
                        inDnd = Dnd.inDnd(CalendarDates.now(), dnd),
                    )
                }
            }
        }
    }

    fun shiftDay(days: Long) {
        val next = CalendarDates.plusDays(_state.value.date, days) ?: return
        _state.update { it.copy(date = next) }
        reload()
    }

    fun today() {
        _state.update { it.copy(date = CalendarDates.todayText()) }
        reload()
    }

    /** 手加一条：时刻留空＝全天项（全天项不参与到点提醒）。 */
    fun add(content: String, time: String, detail: String = "") {
        val text = content.trim()
        if (text.isEmpty()) return
        viewModelScope.launch {
            val date = _state.value.date
            val t = time.trim().takeIf { it.isNotEmpty() }?.let { CalendarDates.normTime(it) }
            if (time.isNotBlank() && t == null) {
                _state.update { it.copy(status = "时刻格式应为 HH:MM") }
                return@launch
            }
            withContext(Dispatchers.IO) {
                store.add(date, text, t, detail = detail)
                scheduler.sync()
            }
            _state.update { it.copy(status = "已加：$date ${t ?: "全天"}「$text」") }
            reload()
        }
    }

    fun toggleDone(item: CalendarItemEntity) {
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                val next = if (item.status == CalendarItemEntity.STATUS_DONE) {
                    CalendarItemEntity.STATUS_TODO
                } else {
                    CalendarItemEntity.STATUS_DONE
                }
                store.setStatus(item.id, next)
                scheduler.sync()
            }
            reload()
        }
    }

    fun cancel(item: CalendarItemEntity, wholeSeries: Boolean = false) {
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                val sid = item.seriesId
                if (wholeSeries && !sid.isNullOrBlank()) store.cancelSeries(sid, item.date)
                else store.cancel(item.id)
                scheduler.sync()
            }
            _state.update { it.copy(status = "已取消 #${item.id}") }
            reload()
        }
    }

    fun setDnd(enabled: Boolean) {
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                dndStore.save(DndSettings(enabled = enabled, untilEpochMillis = null))
                // 关掉免打扰后，之前被顺延的提醒要重新排回原时刻附近
                scheduler.sync()
            }
            reload()
        }
    }

    fun clearStatus() {
        _state.update { it.copy(status = "") }
    }
}
