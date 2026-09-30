package com.xiaozhu.planner.ui.calendar

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.material3.Checkbox
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
    var draftKind by remember { mutableStateOf(CalendarItemEntity.KIND_EVENT) }
    var draftRepeat by remember { mutableStateOf("") }

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
            DayDetail(
                state = state,
                vm = vm,
                draft = draft,
                draftTime = draftTime,
                kind = draftKind,
                repeat = draftRepeat,
                onDraft = { draft = it },
                onTime = { draftTime = it },
                onKind = { draftKind = it },
                onRepeat = { draftRepeat = it },
            )

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
    // PC 版是**周日开头**（index.html 的 cal-weekdays：日一二三四五六），
    // 且 lead = new Date(y, m, 1).getDay()（0=周日）——照抄，别自作主张改成周一
    val weekdays = listOf("日", "一", "二", "三", "四", "五", "六")
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
    val leading = first.dayOfWeek.value % 7          // 周日=0 … 周六=6
    val daysInMonth = state.month.lengthOfMonth()
    val rows = (leading + daysInMonth + 6) / 7

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
                                // PC 版的角标是**未完成的数量**（undone），不是"有几项"
                                undone = state.undoneByDay[date.toString()] ?: 0,
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
    undone: Int,
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
    // 注意这里是两层 Box，不是一层：
    //
    // 之前把"圆形底 + 日期数字 + 右下角角标"放在同一个 Box 里，并且用了
    // `.clip(CircleShape)` —— 圆形裁剪会把**方形容器右下角**的角标切掉，
    // 于是角标只露出圈内那一小块（用户报的"数字显示不完整"）。
    // 现在圆只画在**背景**上（`background(color, shape)` 不裁剪子元素），
    // 角标叠在外层、不再被裁。
    Box(modifier = Modifier.size(38.dp)) {
        Box(
            modifier = Modifier
                .matchParentSize()
                .clip(CircleShape)
                .background(bg)
                .then(
                    if (today && !selected) Modifier.border(1.dp, PlannerColors.Primary, CircleShape)
                    else Modifier
                )
                .clickable(onClick = onClick),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                date.dayOfMonth.toString(),
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = if (selected || today) FontWeight.SemiBold else FontWeight.Normal,
                color = fg,
            )
        }
        // 右下角一个小圆里写数字（PC 版 .cal-dot）
        if (undone > 0) {
            Box(
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .size(16.dp)
                    .clip(CircleShape)
                    .background(if (selected) Color.White else PlannerColors.Accent),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    // 两位数字在 16dp 圆里会挤，超过 9 就写 9+（PC 的悬浮球角标也是这么做的）
                    if (undone > 9) "9+" else undone.toString(),
                    style = MaterialTheme.typography.labelSmall,
                    color = if (selected) PlannerColors.Primary else Color.White,
                )
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
    kind: String,
    repeat: String,
    onDraft: (String) -> Unit,
    onTime: (String) -> Unit,
    onKind: (String) -> Unit,
    onRepeat: (String) -> Unit,
) {
    val date = state.selected
    // PC 版标题格式：`M 月 D 日 · 周X`
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
            value = draftTime,
            onValueChange = onTime,
            label = { Text("时刻") },
            placeholder = { Text("HH:MM") },
            singleLine = true,
            modifier = Modifier.width(96.dp),
        )
        // PC 版的添加表单有 类型 / 重复 两个选择器（index.html 的 cal-kind / cal-repeat）
        KindPicker(kind, onKind)
        RepeatPicker(repeat, onRepeat)
    }
    Row(
        modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        OutlinedTextField(
            value = draft,
            onValueChange = onDraft,
            label = { Text("添加日程或任务") },
            singleLine = true,
            modifier = Modifier.weight(1f),
        )
        Button(onClick = {
            vm.add(draft, draftTime, kind = kind, repeat = repeat)
            onDraft("")
            onTime("")
            onRepeat("")
        }) { Text("添加") }
    }
    Text(
        "时刻留空＝全天项（不到点弹提醒，但议程里看得见）；选了重复就按规则排一串",
        style = MaterialTheme.typography.bodySmall,
        color = PlannerColors.TextSoft,
    )
}

/** 类型：日程 / 任务（PC 版是个 `<select>`）。 */
@Composable
private fun KindPicker(kind: String, onChange: (String) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        listOf(
            CalendarItemEntity.KIND_EVENT to "日程",
            CalendarItemEntity.KIND_TASK to "任务",
        ).forEach { (value, label) ->
            FilterChipLike(selected = kind == value, label = label) { onChange(value) }
        }
    }
}

/** 重复：不重复 / 每天 / 每周 / 工作日（PC 版是个 `<select>`）。 */
@Composable
private fun RepeatPicker(repeat: String, onChange: (String) -> Unit) {
    Row(
        modifier = Modifier.horizontalScroll(rememberScrollState()),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        listOf(
            "" to "不重复",
            "daily" to "每天",
            "weekly" to "每周",
            "weekdays" to "工作日",
        ).forEach { (value, label) ->
            FilterChipLike(selected = repeat == value, label = label) { onChange(value) }
        }
    }
}

/** 一个轻量的"选中态标签"，避免为了两个小选择器引入 Chip 的一堆默认样式。 */
@Composable
private fun FilterChipLike(selected: Boolean, label: String, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .padding(end = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(if (selected) PlannerColors.Primary else PlannerColors.SurfaceSoft)
            .clickable(onClick = onClick)
            .padding(horizontal = 8.dp, vertical = 5.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = if (selected) Color.White else PlannerColors.Text,
        )
    }
}

@Composable
private fun ItemCard(item: CalendarItemEntity, vm: CalendarViewModel, selected: LocalDate) {
    val done = item.status == CalendarItemEntity.STATUS_DONE
    val cancelled = item.status == CalendarItemEntity.STATUS_CANCELLED
    // PC 版一行是：勾选框 + 时刻 + 类型 + 内容 + [↻ 重复] + [✕ 取消这一次]
    var confirmingSeries by remember { mutableStateOf(false) }

    Card(
        modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        colors = CardDefaults.cardColors(
            containerColor = if (done || cancelled) PlannerColors.SurfaceSoft else PlannerColors.Surface
        ),
    ) {
        Column(modifier = Modifier.padding(horizontal = 8.dp, vertical = 6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(checked = done, onCheckedChange = { vm.toggleDone(item) })
                Text(
                    item.time ?: "全天",
                    style = MaterialTheme.typography.labelLarge,
                    color = if (done) PlannerColors.TextSoft else PlannerColors.Primary,
                    modifier = Modifier.width(46.dp),
                )
                Text(
                    if (item.kind == CalendarItemEntity.KIND_TASK) "任务" else "日程",
                    style = MaterialTheme.typography.labelMedium,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.width(32.dp),
                )
                Text(
                    item.content,
                    style = MaterialTheme.typography.bodyMedium,
                    textDecoration = if (done) TextDecoration.LineThrough else null,
                    color = if (done) PlannerColors.TextSoft else PlannerColors.Text,
                    modifier = Modifier.weight(1f),
                )
                if (!item.seriesId.isNullOrBlank()) {
                    Text(
                        "↻" + (AgendaSnapshot.REPEAT_LABELS[item.repeat] ?: "重复"),
                        style = MaterialTheme.typography.labelMedium,
                        color = PlannerColors.Primary,
                        modifier = Modifier
                            .clip(RoundedCornerShape(6.dp))
                            .clickable { confirmingSeries = !confirmingSeries }
                            .padding(horizontal = 6.dp, vertical = 4.dp),
                    )
                }
                Text(
                    "✕",
                    style = MaterialTheme.typography.labelLarge,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .clickable { vm.cancel(item) }
                        .padding(horizontal = 8.dp, vertical = 4.dp),
                )
            }

            if (item.detail.isNotBlank()) {
                Text(
                    item.detail,
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(start = 4.dp),
                )
            }
            if (done) {
                Text(
                    "已完成" + (item.remindedAt?.let { " · 已提醒 $it" } ?: ""),
                    style = MaterialTheme.typography.labelMedium,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(start = 4.dp),
                )
            }

            // 重复项：PC 版点 ↻ 展开一条内联确认（不用系统弹窗）
            if (confirmingSeries) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.padding(start = 4.dp, top = 2.dp),
                ) {
                    Text(
                        "取消该系列自 ${item.date} 起、今后所有未完成的期？",
                        style = MaterialTheme.typography.bodySmall,
                        color = PlannerColors.TextSoft,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(
                        onClick = {
                            confirmingSeries = false
                            vm.cancel(item, wholeSeries = true)
                        },
                        contentPadding = PaddingValues(horizontal = 6.dp),
                    ) { Text("确定", style = MaterialTheme.typography.labelMedium) }
                    TextButton(
                        onClick = { confirmingSeries = false },
                        contentPadding = PaddingValues(horizontal = 6.dp),
                    ) { Text("算了", style = MaterialTheme.typography.labelMedium) }
                }
            }
        }
    }
}

private fun weekdayName(date: LocalDate): String =
    listOf("周一", "周二", "周三", "周四", "周五", "周六", "周日")[date.dayOfWeek.value - 1]
