package com.xiaozhu.planner.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    vm: ChatViewModel,
    onOpenSettings: () -> Unit,
    onOpenMemory: () -> Unit,
    onOpenCalendar: () -> Unit,
) {
    val state by vm.state.collectAsState()
    var draft by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    // 新消息（含流式增量的最后一条）始终滚到底，否则用户看不见正在写的内容
    LaunchedEffect(state.messages.size, state.messages.lastOrNull()?.text) {
        if (state.messages.isNotEmpty()) {
            listState.scrollToItem(state.messages.lastIndex)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("小助") },
                actions = {
                    TextButton(onClick = onOpenMemory) { Text("记忆") }
                    TextButton(onClick = onOpenCalendar) { Text("日历") }
                    TextButton(onClick = onOpenSettings) { Text("设置") }
                },
            )
        }
    ) { pad ->
        Column(modifier = Modifier.fillMaxSize().padding(pad)) {
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(state.messages, key = { it.id }) { msg -> MessageRow(msg) }
            }

            state.memoryNotice?.let { notice ->
                Text(
                    notice,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                )
            }

            state.hint?.let { hint ->
                Text(
                    hint,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                )
            }

            Row(
                modifier = Modifier.fillMaxWidth().padding(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("说点什么…") },
                    maxLines = 4,
                )
                if (state.streaming) {
                    Button(onClick = { vm.stop() }, modifier = Modifier.padding(start = 8.dp)) {
                        Text("停")
                    }
                } else {
                    Button(
                        onClick = { vm.send(draft); draft = "" },
                        modifier = Modifier.padding(start = 8.dp),
                    ) { Text("发") }
                }
            }
        }
    }
}

@Composable
private fun MessageRow(msg: UiMessage) {
    // 系统触发的一条（到点提醒）：居中、浅色、别伪装成用户说的话
    if (msg.fromSystem) {
        Box(modifier = Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            Text(
                "⏰ " + msg.text,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(10.dp))
                    .padding(horizontal = 10.dp, vertical = 6.dp),
            )
        }
        return
    }

    val bubbleColor = if (msg.fromUser) {
        MaterialTheme.colorScheme.primaryContainer
    } else if (msg.failed) {
        MaterialTheme.colorScheme.errorContainer
    } else {
        MaterialTheme.colorScheme.surfaceVariant
    }
    Box(
        modifier = Modifier.fillMaxWidth(),
        contentAlignment = if (msg.fromUser) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        Column(modifier = Modifier.fillMaxWidth(0.92f), horizontalAlignment = if (msg.fromUser) Alignment.End else Alignment.Start) {
            if (msg.text.isNotBlank()) {
                Box(
                    modifier = Modifier
                        .background(bubbleColor, RoundedCornerShape(12.dp))
                        .padding(10.dp)
                ) {
                    Text(msg.text, style = MaterialTheme.typography.bodyMedium)
                }
            }
            msg.tools.forEach { tool -> ToolCard(tool) }
        }
    }
}

@Composable
private fun ToolCard(tool: UiTool) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Text("工具 · ${tool.name}", style = MaterialTheme.typography.labelMedium)
            Text(
                tool.args,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            if (tool.result != null) {
                Text(
                    tool.result,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 4.dp),
                    maxLines = 6,
                    overflow = TextOverflow.Ellipsis,
                )
            } else {
                Text("执行中…", style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}