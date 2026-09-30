package com.xiaozhu.planner.ui.memory

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.xiaozhu.planner.core.memory.NodeRenderer
import com.xiaozhu.planner.ui.chat.ChatViewModel
import com.xiaozhu.planner.ui.chat.MemoryStats
import kotlinx.coroutines.launch

/**
 * 记忆浏览：能看见小助到底记住了什么（否则"记忆"是个黑箱）。
 * 支持按层浏览 + 关键词检索（检索走的是与工具同一条 LIKE 路径）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MemoryScreen(vm: ChatViewModel, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var stats by remember { mutableStateOf<MemoryStats?>(null) }
    var nodes by remember { mutableStateOf(listOf<Pair<String, String>>()) }
    var detail by remember { mutableStateOf<String?>(null) }
    var query by remember { mutableStateOf("") }
    var status by remember { mutableStateOf("正在读取记忆…") }

    LaunchedEffect(Unit) {
        val s = vm.memoryStats()
        stats = s
        status = if (s.totalNodes == 0) {
            "还没有记忆节点。对话累积到 60 条原文时会自动压缩成一个节点。"
        } else {
            "共 ${s.totalNodes} 个节点，未压缩原文 ${s.rawMessages} 条"
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("记忆") },
                actions = { TextButton(onClick = onBack) { Text("返回") } },
            )
        }
    ) { pad ->
        Column(
            modifier = Modifier
                .padding(pad)
                .padding(12.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Text(status, style = MaterialTheme.typography.bodyMedium)
            stats?.rootId?.let {
                Text("根节点：$it", style = MaterialTheme.typography.bodySmall)
            }

            Row(modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it },
                    label = { Text("搜记忆（含叶子原文）") },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = {
                    scope.launch {
                        val hits = vm.memorySearch(query)
                        detail = if (hits.isEmpty()) {
                            "没有找到「$query」。"
                        } else {
                            hits.joinToString("\n\n") { "${it.nodeId}（level ${it.level}）\n${it.snippet}" }
                        }
                    }
                }) { Text("搜") }
            }

            stats?.levels?.forEach { (level, count) ->
                HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))
                Text(
                    "level $level · $count 个活跃节点",
                    style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.clickable {
                        scope.launch {
                            nodes = vm.memoryNodesAt(level).map { it.id to it.summary }
                            detail = null
                        }
                    },
                )
            }

            nodes.forEach { (id, summary) ->
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 6.dp)
                        .clickable {
                            scope.launch {
                                val info = vm.memoryNodeInfo(id)
                                detail = info?.let { NodeRenderer.renderForTool(it) } ?: "找不到 $id"
                            }
                        }
                ) {
                    Column(modifier = Modifier.padding(10.dp)) {
                        Text(id, style = MaterialTheme.typography.labelMedium)
                        Text(
                            summary,
                            style = MaterialTheme.typography.bodySmall,
                            maxLines = 3,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }

            detail?.let {
                HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))
                Text("展开内容", style = MaterialTheme.typography.titleSmall)
                Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}