package com.xiaozhu.planner.ui.calendar

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import com.xiaozhu.planner.core.calendar.AgendaSnapshot
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.ui.theme.PlannerColors
import java.time.LocalDate

/**
 * 日历页：**月视图**（用户明确要求"一个真正的日历"）。
 *
 * 结构：
 * - 月份标题 + 前后翻月 + 回今天
 * - 星期表头（周一起）+ 6×7 日期格子：今天描边、选中填充、有安排的点
 * - 选中那天的清单（完成 / 取消 / 改到这天）
 * - 手加一条 + 重复系列概览 + 夜间免打扰开关
 *
 * 刻意不上第三方日历库：自己画 42 个格子足够，也少一个依赖与一套风格。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CalendarScreen(vm: CalendarViewModel, onBack: () -> Unit) {
    val state by vm.state.collectAsState()
    var draft by remember { mutableStateOf("") }
    var draftTime by remember { mutableStateOf("") }

    Scaffold(
        containerColor = PlannerColors.Background,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(containerColor = PlannerColors.Background),
                title = { Text("日历", style = MaterialTheme.typography.titleMedium) },
                actions = { TextButton(onClick = onBack) { Text("返回") } },
            )
        }
    ) { pad ->
        Column(
            modifier = Modifier
                .padding(pad)
                .padding(horizontal = 12.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            MonthHeader(state, vm)
            MonthGrid(state, vm)
            Spacer(Modifier.height(8.dp))
            DayDetail(state, vm, draft, draftTime, onDraft = { draft = it }, onTime = { draftTime = it })

            state.status.takeIf { it.isNotBlank() }?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.Primary,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }

            if (state.series.isNotEmpty()) {
                HorizontalDivider(modifier = Modifier.padding(vertical = 12.dp))
                Text("重复系列", style = MaterialTheme.typography.titleSmall)
                state.series.forEach { s ->
                    val label = AgendaSnapshot.REPEAT_LABELS[s.repeat] ?: "重复"
                    Text(
                        "「${s.content}」$label ${s.time ?: "全天"} · " +
                            "${s.firstDate} ~ ${s.lastDate}（未完成 ${s.todoCount}/${s.total}）",
                        style = MaterialTheme.typography.bodySmall,
                        color = PlannerColors.TextSoft,
                        modifier = Modifier.padding(top = 2.dp),
                    )
                }
                Text(
                    "重复项是提前排好实体行的；系统会在快排完时自动续期。",
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                )
            }

            HorizontalDivider(modifier = Modifier.padding(vertical = 12.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("夜间免打扰", style = MaterialTheme.typography.titleSmall)
                    Text(
                        if (state.inDnd) "现在处于免打扰：提醒不弹、它也不主动说话（你找它照常回）。"
                        else "默认 ${Dnd.DEFAULT_START_HOUR}:00-${Dnd.DEFAULT_END_HOUR}:00 不主动打扰。",
                        style = MaterialTheme.typography.bodySmall,
                        color = PlannerColors.TextSoft,
                    )
                }
                Switch(checked = state.dnd.enabled, onCheckedChange = { vm.setDnd(it) })
            }
            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable
private fun MonthHeader(state: CalendarUiState, vm: CalendarViewModel) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        TextButton(onClick = { vm.shiftMonth(-1) }) { Text("‹ 上月") }
        Text(
            "${state.month.year} 年 ${state.month.monthValue} 月",
            style = MaterialTheme.typography.titleMedium,
            textAlign = TextAlign.Center,
            modifier = Modifier.weight(1f),
        )
        TextButton(onClick = { vm.goToday() }) { Text("今天") }
        TextButton(onClick = { vm.shiftMonth(1) }) { Text("下月 ›") }
    }
}

@Composable
private fun MonthGrid(state: CalendarUiState, vm: CalendarViewModel) {
    val weekdays = listOf("一", "二", "三", "四", "五", "六", "日")
    Row(modifier = Modifier.fillMaxWidth()) {
        weekdays.forEach { label ->
            Text(
                label,
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
                textAlign = TextAlign.Center,
                modifier = Modifier.weight(1f),
            )
        }
    }
    Spacer(Modifier.height(4.dp))

    val first = state.month.atDay(1)
    // 周一为一周起点：把 1 号之前的空位补出来
    val leading = (first.dayOfWeek.value + 6) % 7
    val daysInMonth = state.month.lengthOfMonth()
    val cells = leading + daysInMonth
    val rows = (cells + 6) / 7

    Column {
        repeat(rows) { row ->
            Row(modifier = Modifier.fillMaxWidth()) {
                repeat(7) { col ->
                    val dayNumber = row * 7 + col - leading + 1
                    Box(modifier = Modifier.weight(1f).aspectRatio(1f), contentAlignment = Alignment.Center) {
                        if (dayNumber in 1..daysInMonth) {
                            val date = state.month.atDay(dayNumber)
                            DayCell(
                                date = date,
                                count = state.countByDay[date.toString()] ?: 0,
                                selected = date == state.selected,
                                today = date == CalendarDates.today(),
                                onClick = { vm.selectDate(date) },
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun DayCell(
    date: LocalDate,
    count: Int,
    selected: Boolean,
    today: Boolean,
    onClick: () -> Unit,
) {
    val bg = when {
        selected -> PlannerColors.Primary
        today -> PlannerColors.Surface
        else -> Color.Transparent
    }
    val fg = when {
        selected -> Color.White
        today -> PlannerColors.Primary
        else -> PlannerColors.Text
    }
    Box(
        modifier = Modifier
            .size(38.dp)
            .clip(CircleShape)
            .background(bg)
            .then(
                if (today && !selected) Modifier.border(1.dp, PlannerColors.Primary, CircleShape)
                else Modifier
            )
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                date.dayOfMonth.toString(),
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = if (selected || today) FontWeight.SemiBold else FontWeight.Normal,
                color = fg,
            )
            if (count > 0) {
                Row(horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                    repeat(minOf(count, 3)) {
                        Box(
                            modifier = Modifier
                                .size(4.dp)
                                .clip(CircleShape)
                                .background(if (selected) Color.White else PlannerColors.Accent),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun DayDetail(
    state: CalendarUiState,
    vm: CalendarViewModel,
    draft: String,
    draftTime: String,
    onDraft: (String) -> Unit,
    onTime: (String) -> Unit,
) {
    val date = state.selected
    Text(
        "${date.monthValue} 月 ${date.dayOfMonth} 日 · " + weekdayName(date) +
            if (date == CalendarDates.today()) "（今天）" else "",
        style = MaterialTheme.typography.titleSmall,
    )

    if (state.dayItems.isEmpty()) {
        Text(
            if (state.loading) "读取中…" else "这一天没有安排。",
            style = MaterialTheme.typography.bodySmall,
            color = PlannerColors.TextSoft,
            modifier = Modifier.padding(vertical = 4.dp),
        )
    }
    state.dayItems.forEach { item -> ItemCard(item, vm, selected = state.selected) }

    Row(
        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        OutlinedTextField(
            value = draft,
            onValueChange = onDraft,
            label = { Text("加一条") },
            singleLine = true,
            modifier = Modifier.weight(1f),
        )
        OutlinedTextField(
            value = draftTime,
            onValueChange = onTime,
            label = { Text("HH:MM") },
            singleLine = true,
            modifier = Modifier.width(104.dp),
        )
        Button(onClick = {
            vm.add(draft, draftTime)
            onDraft("")
            onTime("")
        }) { Text("加") }
    }
    Text(
        "时刻留空＝全天项（不到点弹提醒，但议程里看得见）",
        style = MaterialTheme.typography.bodySmall,
        color = PlannerColors.TextSoft,
    )
}

@Composable
private fun ItemCard(item: CalendarItemEntity, vm: CalendarViewModel, selected: LocalDate) {
    val done = item.status == CalendarItemEntity.STATUS_DONE
    val cancelled = item.status == CalendarItemEntity.STATUS_CANCELLED
    Card(
        modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        colors = CardDefaults.cardColors(
            containerColor = if (done || cancelled) PlannerColors.SurfaceSoft else PlannerColors.Surface
        ),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    item.time ?: "全天",
                    style = MaterialTheme.typography.labelLarge,
                    color = if (done || cancelled) PlannerColors.TextSoft else PlannerColors.Primary,
                    modifier = Modifier.width(48.dp),
                )
                Text(
                    item.content,
                    style = MaterialTheme.typography.bodyMedium,
                    textDecoration = if (done || cancelled) TextDecoration.LineThrough else null,
                    color = if (done || cancelled) PlannerColors.TextSoft else PlannerColors.Text,
                    modifier = Modifier.weight(1f),
                )
            }
            val marks = buildList {
                if (item.kind == CalendarItemEntity.KIND_TASK) add("任务")
                item.seriesId?.takeIf { it.isNotBlank() }?.let {
                    add(AgendaSnapshot.REPEAT_LABELS[item.repeat] ?: "重复")
                }
                if (done) add("已完成")
                if (cancelled) add("已取消")
                item.remindedAt?.let { add("已提醒 $it") }
            }
            if (marks.isNotEmpty()) {
                Text(
                    marks.joinToString(" · "),
                    style = MaterialTheme.typography.labelMedium,
                    color = PlannerColors.TextSoft,
                )
            }
            if (item.detail.isNotBlank()) {
                Text(item.detail, style = MaterialTheme.typography.bodySmall, color = PlannerColors.TextSoft)
            }
            if (!cancelled) {
                Row(horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                    TextButton(
                        onClick = { vm.toggleDone(item) },
                        contentPadding = PaddingValues(horizontal = 6.dp),
                    ) { Text(if (done) "撤销完成" else "完成", style = MaterialTheme.typography.labelMedium) }

                    TextButton(
                        onClick = { vm.cancel(item) },
                        contentPadding = PaddingValues(horizontal = 6.dp),
                    ) { Text("取消", style = MaterialTheme.typography.labelMedium) }

                    if (item.date != selected.toString()) {
                        TextButton(
                            onClick = { vm.moveToSelected(item) },
                            contentPadding = PaddingValues(horizontal = 6.dp),
                        ) { Text("改到这天", style = MaterialTheme.typography.labelMedium) }
                    }
                    if (!item.seriesId.isNullOrBlank()) {
                        TextButton(
                            onClick = { vm.cancel(item, wholeSeries = true) },
                            contentPadding = PaddingValues(horizontal = 6.dp),
                        ) { Text("取消整个系列", style = MaterialTheme.typography.labelMedium) }
                    }
                }
            }
        }
    }
}

private fun weekdayName(date: LocalDate): String =
    listOf("周一", "周二", "周三", "周四", "周五", "周六", "周日")[date.dayOfWeek.value - 1]
