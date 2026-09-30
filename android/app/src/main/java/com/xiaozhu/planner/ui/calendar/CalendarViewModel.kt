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
import java.time.LocalDate
import java.time.YearMonth

data class CalendarUiState(
    /** 正在看的月份。 */
    val month: YearMonth = YearMonth.now(CalendarDates.ZONE),
    /** 选中的那一天（默认今天）。 */
    val selected: LocalDate = CalendarDates.today(),
    /** 整月的日程（用于画点）；不含已取消。 */
    val monthItems: List<CalendarItemEntity> = emptyList(),
    val series: List<SeriesRow> = emptyList(),
    val dnd: DndSettings = DndSettings(),
    val inDnd: Boolean = false,
    val status: String = "",
    val loading: Boolean = true,
) {
    /** 选中那天的日程。 */
    val dayItems: List<CalendarItemEntity>
        get() = monthItems.filter { it.date == selected.toString() }

    /** 每天有几项（画点用）。 */
    val countByDay: Map<String, Int>
        get() = monthItems.groupingBy { it.date }.eachCount()
}

/**
 * 日历页：**真正的月历**（用户明确要的），不是"一天一列表"。
 *
 * 与模型共用同一份 `calendar_items`：这边手加的，它 `list_schedule` 就能查到；
 * 它加的，这边立刻看得见。月历的圆点表示"这天有安排"，点某天在下面看细节。
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
            val month = _state.value.month
            withContext(Dispatchers.IO) {
                val first = month.atDay(1).toString()
                val last = month.atEndOfMonth().toString()
                val items = store.range(first, last)
                val series = store.seriesSummary()
                val dnd = dndStore.load()
                _state.update {
                    it.copy(
                        monthItems = items,
                        series = series,
                        dnd = dnd,
                        inDnd = Dnd.inDnd(CalendarDates.now(), dnd),
                        loading = false,
                    )
                }
            }
        }
    }

    fun shiftMonth(delta: Long) {
        _state.update { it.copy(month = it.month.plusMonths(delta), loading = true) }
        reload()
    }

    fun selectDate(date: LocalDate) {
        _state.update { it.copy(selected = date) }
    }

    fun goToday() {
        _state.update { it.copy(month = YearMonth.now(CalendarDates.ZONE), selected = CalendarDates.today()) }
        reload()
    }

    /** 手加一条到**选中的那天**；时刻留空＝全天项（不参与到点提醒）。 */
    fun add(content: String, time: String, detail: String = "") {
        val text = content.trim()
        if (text.isEmpty()) return
        viewModelScope.launch {
            val date = _state.value.selected.toString()
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

    /** 改期到选中那天（拖不动，就用"改到这天"按钮）。 */
    fun moveToSelected(item: CalendarItemEntity) {
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                store.update(id = item.id, date = _state.value.selected.toString())
                scheduler.sync()
            }
            _state.update { it.copy(status = "已把 #${item.id} 改到 ${_state.value.selected}") }
            reload()
        }
    }

    fun setDnd(enabled: Boolean) {
        viewModelScope.launch {
            withContext(Dispatchers.IO) {
                dndStore.save(DndSettings(enabled = enabled, untilEpochMillis = null))
                scheduler.sync()
            }
            reload()
        }
    }

    fun clearStatus() {
        _state.update { it.copy(status = "") }
    }
}
