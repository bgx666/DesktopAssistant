package com.xiaozhu.planner.ui.chat

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.xiaozhu.planner.speech.VoiceInput
import com.xiaozhu.planner.ui.theme.PlannerColors

/**
 * 聊天页。视觉与交互都对着 PC 版来：
 *
 * - 小助的头像 = PC 版那个青绿渐变球（`.avatar`，CSS 画的那个）
 * - 用户气泡：135° 蓝色渐变、白字、右下角小圆角（`.bubble.user`）
 * - 小助气泡：浅色玻璃感、左下角小圆角（`.bubble.assistant`）
 * - 记忆摘要：顶部虚线卡片（`.msg.memory`）——**这是"它记得"唯一看得见的地方**
 * - 系统消息：居中灰字（`.msg.log`）
 * - 用户消息带「撤销」：两段确认，内容回到输入框（PC 版 `/undo` 的行为）
 *
 * 顶栏用图标而不是文字：三个中文按钮在窄屏上会被截断（用户报过）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    vm: ChatViewModel,
    onOpenSettings: () -> Unit,
    onOpenMemory: () -> Unit,
    onOpenCalendar: () -> Unit,
    onOpenImport: () -> Unit,
) {
    val state by vm.state.collectAsState()
    var draft by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    // 撤销后把内容放回输入框（PC 版行为：改完重发，而不是直接丢掉）
    LaunchedEffect(state.restoreDraft) {
        state.restoreDraft?.let {
            draft = it
            vm.consumeRestoreDraft()
        }
    }

    val context = LocalContext.current
    var listening by remember { mutableStateOf(false) }
    val voice = remember {
        VoiceInput(
            context = context,
            onPartial = { draft = it },
            onFinal = { draft = it },
            onError = { vm.showHint(it) },
            onStateChange = { listening = it },
        )
    }
    DisposableEffect(Unit) { onDispose { voice.destroy() } }

    val micPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) voice.start() else vm.showHint("没有麦克风权限，语音输入用不了；打字也一样。")
    }

    LaunchedEffect(state.messages.size, state.messages.lastOrNull()?.text) {
        if (state.messages.isNotEmpty()) listState.scrollToItem(state.messages.lastIndex)
    }

    Scaffold(
        containerColor = PlannerColors.Background,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = PlannerColors.Background,
                ),
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        AssistantAvatar(size = 26.dp)
                        Spacer(Modifier.width(8.dp))
                        Column {
                            Text("小助", style = MaterialTheme.typography.titleMedium)
                            Text(
                                memoryLine(state.memory),
                                style = MaterialTheme.typography.bodySmall,
                                color = PlannerColors.TextSoft,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                },
                actions = {
                    // 图标 + 短标签：纯中文标签在窄屏会被截断
                    IconAction("🗓", "日历", onOpenCalendar)
                    IconAction("🧠", "记忆", onOpenMemory)
                    IconAction("⚙", "设置", onOpenSettings)
                },
            )
        }
    ) { pad ->
        Column(modifier = Modifier.fillMaxSize().padding(pad)) {
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                if (!state.historyLoaded) {
                    item { CenterNote("正在读取记忆…") }
                } else if (state.messages.isEmpty()) {
                    item {
                        Column(
                            modifier = Modifier.fillMaxWidth().padding(top = 24.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            AssistantAvatar(size = 48.dp)
                            Spacer(Modifier.height(10.dp))
                            Text("还没有对话", style = MaterialTheme.typography.titleSmall)
                            Spacer(Modifier.height(6.dp))
                            Text(
                                "电脑上那份记忆与日历可以搬过来：\n" +
                                    "在电脑上跑 scripts\\export_to_android.py，\n" +
                                    "把生成的 JSON 传到手机、点它选「小助」即可。",
                                style = MaterialTheme.typography.bodySmall,
                                color = PlannerColors.TextSoft,
                                textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                            )
                            Spacer(Modifier.height(10.dp))
                            Button(onClick = onOpenImport) { Text("导入电脑上的记忆") }
                        }
                    }
                }
                items(state.messages, key = { it.id }) { msg ->
                    MessageRow(
                        msg = msg,
                        onSpeak = vm::speak,
                        onUndo = { vm.undo(msg.id) },
                    )
                }
            }

            state.memoryNotice?.let {
                BannerText(it, PlannerColors.Primary)
            }
            state.hint?.let {
                BannerText(it, PlannerColors.Danger)
            }

            Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
                verticalAlignment = Alignment.Bottom,
            ) {
                TextButton(
                    onClick = {
                        if (listening) {
                            voice.stop()
                        } else if (ContextCompat.checkSelfPermission(
                                context, Manifest.permission.RECORD_AUDIO
                            ) == PackageManager.PERMISSION_GRANTED
                        ) {
                            voice.start()
                        } else {
                            micPermission.launch(Manifest.permission.RECORD_AUDIO)
                        }
                    },
                    modifier = Modifier.padding(bottom = 4.dp),
                ) { Text(if (listening) "停" else "🎤", style = MaterialTheme.typography.titleMedium) }

                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text(if (listening) "在听…" else "说点什么…") },
                    maxLines = 4,
                    shape = RoundedCornerShape(14.dp),
                )

                Spacer(Modifier.width(8.dp))
                if (state.streaming) {
                    Button(
                        onClick = { vm.stop() },
                        modifier = Modifier.padding(bottom = 4.dp),
                    ) { Text("停") }
                } else {
                    Button(
                        onClick = { vm.send(draft); draft = "" },
                        enabled = draft.isNotBlank(),
                        modifier = Modifier.padding(bottom = 4.dp),
                    ) { Text("发送") }
                }
            }
        }
    }
}

/** 顶栏图标按钮：图标在上、小字在下，宽度固定，不会被挤到截断。 */
@Composable
private fun IconAction(icon: String, label: String, onClick: () -> Unit) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .clickable(onClick = onClick)
            .padding(horizontal = 6.dp, vertical = 4.dp),
    ) {
        Text(icon, style = MaterialTheme.typography.titleMedium)
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = PlannerColors.TextSoft,
        )
    }
}

/** PC 版那个渐变球（`.avatar`：radial-gradient + 中间一个白点）。 */
@Composable
fun AssistantAvatar(size: androidx.compose.ui.unit.Dp = 28.dp) {
    Box(
        modifier = Modifier
            .size(size)
            .clip(CircleShape)
            .background(PlannerColors.AvatarGradient),
        contentAlignment = Alignment.Center,
    ) {
        Box(
            modifier = Modifier
                .size(size * 0.32f)
                .clip(CircleShape)
                .background(Color.White)
        )
    }
}

@Composable
private fun MessageRow(
    msg: UiMessage,
    onSpeak: (String) -> Unit,
    onUndo: () -> Unit,
) {
    when (msg.kind) {
        UiMessageKind.System -> CenterNote(msg.text)
        UiMessageKind.Memory -> MemoryCard(msg)
        UiMessageKind.User -> UserRow(msg, onUndo)
        UiMessageKind.Assistant -> AssistantRow(msg, onSpeak)
    }
}

/** 用户消息：右侧蓝色渐变气泡 + 撤销按钮。 */
@Composable
private fun UserRow(msg: UiMessage, onUndo: () -> Unit) {
    var confirming by remember { mutableStateOf(false) }

    // 两段确认（PC 版同款）：第一次点变成"确认撤销？"，3 秒内再点才执行
    LaunchedEffect(confirming) {
        if (confirming) {
            kotlinx.coroutines.delay(3000)
            confirming = false
        }
    }

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.End,
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth(0.86f)
                .clip(RoundedCornerShape(14.dp, 14.dp, 5.dp, 14.dp))
                .background(PlannerColors.UserBubble)
                .padding(horizontal = 13.dp, vertical = 9.dp),
        ) {
            Text(
                msg.text,
                style = MaterialTheme.typography.bodyMedium,
                color = Color.White,
            )
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            msg.time?.let {
                Text(it, style = MaterialTheme.typography.labelMedium, color = PlannerColors.TextSoft)
            }
            TextButton(onClick = {
                if (confirming) {
                    confirming = false
                    onUndo()
                } else {
                    confirming = true
                }
            }) {
                Text(
                    if (confirming) "确认撤销？" else "撤销",
                    style = MaterialTheme.typography.labelMedium,
                    color = if (confirming) PlannerColors.Danger else PlannerColors.TextSoft,
                )
            }
        }
    }
}

/** 小助消息：头像 + 浅色气泡 + 工具卡片 + 朗读。 */
@Composable
private fun AssistantRow(msg: UiMessage, onSpeak: (String) -> Unit) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            AssistantAvatar()
            Spacer(Modifier.width(6.dp))
            Text(
                "小助",
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
            )
            msg.time?.let {
                Spacer(Modifier.width(6.dp))
                Text(it, style = MaterialTheme.typography.labelMedium, color = PlannerColors.TextSoft)
            }
        }
        Spacer(Modifier.height(4.dp))
        Box(
            modifier = Modifier
                .fillMaxWidth(0.92f)
                .clip(RoundedCornerShape(14.dp, 14.dp, 14.dp, 5.dp))
                .background(if (msg.failed) Color(0xFFFFECEF) else PlannerColors.Surface)
                .border(1.dp, Color(0x22000000), RoundedCornerShape(14.dp, 14.dp, 14.dp, 5.dp))
                .padding(horizontal = 13.dp, vertical = 9.dp),
        ) {
            if (msg.text.isBlank() && msg.tools.isEmpty()) {
                Text("…", style = MaterialTheme.typography.bodyMedium, color = PlannerColors.TextSoft)
            } else {
                Text(
                    msg.text,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (msg.failed) PlannerColors.Danger else PlannerColors.Text,
                )
            }
        }
        msg.tools.forEach { ToolCard(it) }
        if (msg.text.isNotBlank() && !msg.failed) {
            TextButton(
                onClick = { onSpeak(msg.text) },
                contentPadding = PaddingValues(horizontal = 4.dp),
            ) {
                Text("🔊 朗读", style = MaterialTheme.typography.labelMedium, color = PlannerColors.TextSoft)
            }
        }
    }
}

/** 记忆卡片：PC 版 `.msg.memory` 的虚线框（Compose 画虚线要自绘，这里用浅蓝底 + 蓝边表达）。 */
@Composable
private fun MemoryCard(msg: UiMessage) {
    Column(modifier = Modifier.fillMaxWidth()) {
        msg.memoryMeta?.let {
            Text(
                it,
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
                modifier = Modifier.padding(bottom = 4.dp),
            )
        }
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(10.dp))
                .background(PlannerColors.MemoryCard)
                .border(1.dp, PlannerColors.MemoryBorder, RoundedCornerShape(10.dp))
                .padding(10.dp),
        ) {
            Text(
                msg.text,
                style = MaterialTheme.typography.bodySmall,
                color = PlannerColors.TextSoft,
            )
        }
    }
}

@Composable
private fun ToolCard(tool: UiTool) {
    Card(
        modifier = Modifier.fillMaxWidth(0.92f).padding(top = 6.dp),
        colors = CardDefaults.cardColors(containerColor = PlannerColors.Surface),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Text("🔧 ${tool.name}", style = MaterialTheme.typography.labelMedium, color = PlannerColors.Primary)
            if (tool.args.isNotBlank()) {
                Text(
                    tool.args,
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            if (tool.result != null) {
                Text(
                    tool.result,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 4.dp),
                    maxLines = 6,
                    overflow = TextOverflow.Ellipsis,
                )
            } else {
                Text(
                    "执行中…",
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
        }
    }
}

@Composable
private fun CenterNote(text: String) {
    Box(modifier = Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        Text(
            text,
            style = MaterialTheme.typography.bodySmall,
            color = PlannerColors.TextSoft,
            modifier = Modifier.padding(vertical = 4.dp),
        )
    }
}

@Composable
private fun BannerText(text: String, color: Color) {
    Text(
        text,
        style = MaterialTheme.typography.bodySmall,
        color = color,
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 2.dp),
    )
}

/** 顶栏副标题：让"它记得多少"一眼可见（用户以为记忆丢了，就是因为这里什么都不显示）。 */
private fun memoryLine(stats: MemoryStats): String = when {
    stats.totalNodes == 0 && stats.rawMessages == 0 -> "还没有记忆"
    stats.totalNodes == 0 -> "对话 ${stats.rawMessages} 条"
    else -> "记忆 ${stats.totalNodes} 个节点 · 对话 ${stats.rawMessages} 条"
}

@Suppress("unused")
private val unusedWeight = FontWeight.Medium
