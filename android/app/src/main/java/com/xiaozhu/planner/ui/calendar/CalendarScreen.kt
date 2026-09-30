package com.xiaozhu.planner.ui.calendar

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import com.xiaozhu.planner.core.calendar.AgendaSnapshot
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.Dnd
import com.xiaozhu.planner.data.db.CalendarItemEntity

/**
 * 日历页。日程是用户和模型**共用**的一份数据，所以这页的重点是"看得见、改得动"：
 * 勾完成、取消、手加一条、翻日期，都在这里做。
 *
 * 没上日期选择器——左右翻 + 回今天足够日常用，少一个依赖少一处出错。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CalendarScreen(vm: CalendarViewModel, onBack: () -> Unit) {
    val state by vm.state.collectAsState()
    var draft by remember { mutableStateOf("") }
    var draftTime by remember { mutableStateOf("") }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("日历") },
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
            // ── 日期 ────────────────────────────────────────
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                TextButton(onClick = { vm.shiftDay(-1) }) { Text("前一天") }
                Text(
                    state.date + if (state.date == CalendarDates.todayText()) "（今天）" else "",
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = { vm.today() }) { Text("今天") }
                TextButton(onClick = { vm.shiftDay(1) }) { Text("后一天") }
            }

            // ── 手加一条 ────────────────────────────────────
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    label = { Text("加一条") },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = draftTime,
                    onValueChange = { draftTime = it },
                    label = { Text("HH:MM") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(0.28f),
                )
                TextButton(onClick = {
                    vm.add(draft, draftTime)
                    draft = ""
                    draftTime = ""
                }) { Text("加") }
            }
            Text(
                "留空时刻＝全天项（不会到点弹提醒，但议程里看得见）",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            state.status.takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
            }

            // ── 当天的项 ────────────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))
            if (state.items.isEmpty()) {
                Text("这一天没有安排。", style = MaterialTheme.typography.bodyMedium)
            }
            state.items.forEach { item -> ItemCard(item, vm) }

            // ── 重复系列 ────────────────────────────────────
            if (state.series.isNotEmpty()) {
                HorizontalDivider(modifier = Modifier.padding(vertical = 10.dp))
                Text("重复系列", style = MaterialTheme.typography.titleSmall)
                state.series.forEach { s ->
                    val label = AgendaSnapshot.REPEAT_LABELS[s.repeat] ?: "重复"
                    Text(
                        "「${s.content}」$label ${s.time ?: "全天"} · " +
                            "${s.firstDate} ~ ${s.lastDate}（未完成 ${s.todoCount}/${s.total}）",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(top = 2.dp),
                    )
                }
                Text(
                    "重复项是提前排好实体行的；系统会在快排完时自动续期。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            // ── 免打扰 ──────────────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 10.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("夜间免打扰", style = MaterialTheme.typography.titleSmall)
                    Text(
                        if (state.inDnd) "现在处于免打扰：提醒不弹、它也不主动说话（你找它照常回）。"
                        else "默认 ${Dnd.DEFAULT_START_HOUR}:00-" +
                            "${Dnd.DEFAULT_END_HOUR}:00 不主动打扰。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(checked = state.dnd.enabled, onCheckedChange = { vm.setDnd(it) })
            }
            Text(
                "关掉之后夜间也会提醒（到点提醒仍需要通知权限）。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(bottom = 16.dp),
            )
        }
    }
}

@Composable
private fun ItemCard(item: CalendarItemEntity, vm: CalendarViewModel) {
    val done = item.status == CalendarItemEntity.STATUS_DONE
    val cancelled = item.status == CalendarItemEntity.STATUS_CANCELLED
    Card(
        modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        colors = CardDefaults.cardColors(
            containerColor = when {
                cancelled -> MaterialTheme.colorScheme.surfaceVariant
                done -> MaterialTheme.colorScheme.surfaceVariant
                else -> MaterialTheme.colorScheme.surface
            }
        ),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    item.time ?: "全天",
                    style = MaterialTheme.typography.labelLarge,
                    modifier = Modifier.padding(end = 8.dp),
                )
                Text(
                    item.content,
                    style = MaterialTheme.typography.bodyMedium,
                    textDecoration = if (done || cancelled) TextDecoration.LineThrough else null,
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
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (item.detail.isNotBlank()) {
                Text(item.detail, style = MaterialTheme.typography.bodySmall)
            }
            if (!cancelled) {
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    TextButton(onClick = { vm.toggleDone(item) }) {
                        Text(if (done) "撤销完成" else "完成")
                    }
                    TextButton(onClick = { vm.cancel(item) }) { Text("取消") }
                    if (!item.seriesId.isNullOrBlank()) {
                        TextButton(onClick = { vm.cancel(item, wholeSeries = true) }) { Text("整个系列取消") }
                    }
                }
            }
        }
    }
}
