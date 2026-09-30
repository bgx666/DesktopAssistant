package com.xiaozhu.planner.ui.chat

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
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
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
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
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.xiaozhu.planner.core.image.ImagePreparer
import com.xiaozhu.planner.speech.VoiceInput
import com.xiaozhu.planner.ui.theme.PlannerColors
import kotlinx.coroutines.launch

/**
 * 聊天页。视觉与交互都对着 PC 版来： *
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
    val scope = rememberCoroutineScope()

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

    // 相册：用系统照片选择器（不需要读存储权限）
    val gallery = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri -> if (uri != null) vm.attachImage(uri) }

    // 拍照：拍完写进我们给的临时文件，从那里读全分辨率原图
    var photoUri by remember { mutableStateOf<android.net.Uri?>(null) }
    val camera = rememberLauncherForActivityResult(
        ActivityResultContracts.TakePicture()
    ) { ok ->
        val uri = photoUri
        photoUri = null
        if (ok && uri != null) vm.attachImage(uri) else if (!ok) vm.showHint("没有拍照。")
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
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text("小助", style = MaterialTheme.typography.titleMedium)
                                // PC 版标题栏的「思考中…」
                                if (state.streaming) {
                                    Spacer(Modifier.width(6.dp))
                                    Text(
                                        "思考中…",
                                        style = MaterialTheme.typography.labelMedium,
                                        color = PlannerColors.Primary,
                                    )
                                }
                            }
                            Text(
                                statusLine(state),
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
                    Box {
                        IconAction("🗓", "日历", onOpenCalendar)
                        // PC 版"日历"页签上的逾期角标
                        if (state.overdueCount > 0) {
                            Box(
                                modifier = Modifier
                                    .align(Alignment.TopEnd)
                                    .padding(top = 2.dp, end = 2.dp)
                                    .size(16.dp)
                                    .clip(CircleShape)
                                    .background(PlannerColors.Danger),
                                contentAlignment = Alignment.Center,
                            ) {
                                Text(
                                    state.overdueCount.toString(),
                                    style = MaterialTheme.typography.labelMedium,
                                    color = Color.White,
                                )
                            }
                        }
                    }
                    IconAction("🧠", "记忆", onOpenMemory)
                    IconAction("⚙", "设置", onOpenSettings)
                },
            )
        }
    ) { pad ->
        Box(modifier = Modifier.fillMaxSize().padding(pad)) {
            Column(modifier = Modifier.fillMaxSize()) {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(9.dp),
                ) {
                    if (!state.historyLoaded) {
                        item { CenterNote("正在读取记忆…") }
                    } else if (state.messages.isEmpty()) {
                        item { EmptyState(onOpenImport) }
                    }
                    // 包里带了旧数据但没导（库非空被自动跳过）→ 问一句，别让用户蒙在鼓里
                    state.seedPrompt?.let { prompt ->
                        item {
                            SeedPromptCard(
                                prompt = prompt,
                                onImport = { vm.importSeedNow() },
                                onDismiss = { vm.dismissSeedPrompt() },
                            )
                        }
                    }
                    items(state.messages, key = { it.id }) { msg ->
                        MessageRow(
                            msg = msg,
                            onSpeak = vm::speak,
                            onUndo = { vm.undo(msg.id) },
                            // 正在流式输出的那条用纯文本渲染（PC 版同款）
                            streaming = state.streaming && msg.id == state.messages.lastOrNull()?.id,
                        )
                    }
                }

                state.memoryNotice?.let { BannerText(it, PlannerColors.Primary) }
                state.hint?.let { BannerText(it, PlannerColors.Danger) }

                InputBar(
                    draft = draft,
                    onDraft = { draft = it },
                    listening = listening,
                    streaming = state.streaming,
                    pendingImages = state.pendingImages,
                    onMic = {
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
                    onPickImage = {
                        // 系统相册选择器（Android 13+ 是照片选择器，不需要读存储权限）
                        gallery.launch(
                            androidx.activity.result.PickVisualMediaRequest(
                                ActivityResultContracts.PickVisualMedia.ImageOnly
                            )
                        )
                    },
                    onTakePhoto = {
                        // 用系统相机拍，输出到 FileProvider 给的临时文件——
                        // 这样拿到的是**全分辨率**原图，而不是 TakePicturePreview 的小缩略图
                        val uri = runCatching { newPhotoUri(context) }.getOrNull()
                        if (uri == null) {
                            vm.showHint("没法创建拍照用的临时文件。")
                        } else {
                            photoUri = uri
                            camera.launch(uri)
                        }
                    },
                    onRemoveImage = { vm.removePendingImage(it) },
                    onSend = { vm.send(draft); draft = "" },
                    onStop = { vm.stop() },
                )
            }

            // PC 版：离开底部 100px 以上才出现「回到最新消息」
            val awayFromBottom by remember {
                derivedStateOf {
                    val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()
                    last != null && last.index < state.messages.lastIndex
                }
            }
            if (awayFromBottom) {
                Box(
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(end = 14.dp, bottom = 96.dp)
                        .size(34.dp)
                        .clip(CircleShape)
                        .background(PlannerColors.Surface)
                        .border(1.dp, Color(0x28141E32), CircleShape)
                        .clickable {
                            scope.launch {
                                if (state.messages.isNotEmpty()) {
                                    listState.animateScrollToItem(state.messages.lastIndex)
                                }
                            }
                        },
                    contentAlignment = Alignment.Center,
                ) {
                    Text("↓", style = MaterialTheme.typography.titleMedium, color = PlannerColors.Primary)
                }
            }
        }
    }
}

/** PC 版标题栏的状态行：记忆概览 + token 估算 + 心跳倒计时。 */
private fun statusLine(state: ChatUiState): String = buildString {
    append(memoryLine(state.memory))
    if (state.contextTokens > 0) {
        append(" · 约 ").append(state.contextTokens).append(" token")
    }
    state.heartbeatLabel?.let { append(" · ").append(it) }
}

@Composable
private fun EmptyState(onOpenImport: () -> Unit) {    Column(
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

/** 输入区。PC 版发送按钮有三态：发送 / 停止（红）/ 录音（红，长按说话时）。
 * 安卓这边语音是独立的麦克风按钮（没有"长按发送键"这个手势），
 * 所以保留两态 + 录音态的高亮。
 */
@Composable
private fun SeedPromptCard(
    prompt: SeedPrompt,
    onImport: () -> Unit,
    onDismiss: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = PlannerColors.MemoryCard),
        border = androidx.compose.foundation.BorderStroke(1.dp, PlannerColors.MemoryBorder),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text("这个安装包里带了电脑上的旧数据", style = MaterialTheme.typography.titleSmall)
            Text(
                "包里：记忆节点 ${prompt.nodes} 个、日程 ${prompt.calendar} 项。\n" +
                    "手机上现在：记忆节点 ${prompt.currentNodes} 个、对话 ${prompt.currentMessages} 条。\n" +
                    "导入会**先清空手机上这些**，再换成包里的（第一次装机选它）。",
                style = MaterialTheme.typography.bodySmall,
                color = PlannerColors.TextSoft,
                modifier = Modifier.padding(top = 4.dp),
            )
            Row(modifier = Modifier.padding(top = 6.dp)) {
                Button(onClick = onImport) { Text("导入并替换") }
                TextButton(
                    onClick = onDismiss,
                    modifier = Modifier.padding(start = 8.dp),
                ) { Text("先不用") }
            }
        }
    }
}

/**
 * 一排图片缩略图（消息里显示用）。
 *
 * 自己拍的/选的图已经缩到长边 1568，直接解码显示即可；
 * 解码失败就跳过（不显示破图），不影响这条消息的文字。
 */
@Composable
private fun ImageStrip(images: List<String>, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        images.take(3).forEach { url ->
            val bitmap = remember(url) {
                runCatching { ImagePreparer.decodeDataUrl(url) }.getOrNull()
            }
            if (bitmap != null) {
                Image(
                    bitmap = bitmap.asImageBitmap(),
                    contentDescription = "图片",
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .size(96.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .border(1.dp, Color(0x22000000), RoundedCornerShape(10.dp)),
                )
            }
        }
        if (images.size > 3) {
            Text(
                "+${images.size - 3}",
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
                modifier = Modifier.align(Alignment.Bottom),
            )
        }
    }
}

/** 待发送的图片：缩略图 + 右上角 ✕ 移除。 */
@Composable
private fun PendingImageStrip(images: List<String>, onRemove: (Int) -> Unit) {
    Row(
        modifier = Modifier.padding(bottom = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        images.forEachIndexed { index, url ->
            val bitmap = remember(url) {
                runCatching { ImagePreparer.decodeDataUrl(url) }.getOrNull()
            }
            Box {
                if (bitmap != null) {
                    Image(
                        bitmap = bitmap.asImageBitmap(),
                        contentDescription = "待发送的图片",
                        contentScale = ContentScale.Crop,
                        modifier = Modifier
                            .size(56.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .border(1.dp, PlannerColors.Primary, RoundedCornerShape(8.dp)),
                    )
                }
                Box(
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .size(18.dp)
                        .clip(CircleShape)
                        .background(Color(0xCC2A3040))
                        .clickable { onRemove(index) },
                    contentAlignment = Alignment.Center,
                ) {
                    Text("✕", style = MaterialTheme.typography.labelSmall, color = Color.White)
                }
            }
        }
    }
}

/**
 * 输入区。PC 版发送按钮有三态：发送 / 停止（红）/ 录音（红，长按说话时）。
 * 安卓这边语音是独立的麦克风按钮（没有"长按发送键"这个手势），
 * 所以保留两态 + 录音态的高亮；发图是 ＋ 按钮（拍照 / 相册）。
 */
@Composable
private fun InputBar(
    draft: String,
    onDraft: (String) -> Unit,
    listening: Boolean,
    streaming: Boolean,
    pendingImages: List<String>,
    onMic: () -> Unit,
    onPickImage: () -> Unit,
    onTakePhoto: () -> Unit,
    onRemoveImage: (Int) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
) {
    var attachMenu by remember { mutableStateOf(false) }

    Column(modifier = Modifier.fillMaxWidth()) {
        // 已选待发的图（发送后清空，与 PC 版"发完清除挂载"同义）
        if (pendingImages.isNotEmpty()) {
            PendingImageStrip(pendingImages, onRemove = onRemoveImage)
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
            verticalAlignment = Alignment.Bottom,
        ) {
            Box(
                modifier = Modifier
                    .padding(bottom = 4.dp)
                    .size(42.dp)
                    .clip(RoundedCornerShape(12.dp))
                    .background(if (listening) PlannerColors.Danger else PlannerColors.Surface)
                    .border(1.dp, Color(0x28141E32), RoundedCornerShape(12.dp))
                    .clickable(onClick = onMic),
                contentAlignment = Alignment.Center,
            ) {
                Text("🎤", style = MaterialTheme.typography.titleMedium)
            }

            // 发图：拍照 / 从相册选（DeepSeek 的 deepseek-flash 支持图片输入）
            Box(modifier = Modifier.padding(bottom = 4.dp)) {
                Box(
                    modifier = Modifier
                        .padding(start = 6.dp)
                        .size(42.dp)
                        .clip(RoundedCornerShape(12.dp))
                        .background(PlannerColors.Surface)
                        .border(1.dp, Color(0x28141E32), RoundedCornerShape(12.dp))
                        .clickable { attachMenu = true },
                    contentAlignment = Alignment.Center,
                ) {
                    Text("＋", style = MaterialTheme.typography.titleMedium)
                }
                DropdownMenu(expanded = attachMenu, onDismissRequest = { attachMenu = false }) {
                    DropdownMenuItem(
                        text = { Text("拍照") },
                        onClick = { attachMenu = false; onTakePhoto() },
                    )
                    DropdownMenuItem(
                        text = { Text("从相册选") },
                        onClick = { attachMenu = false; onPickImage() },
                    )
                }
            }

            OutlinedTextField(
                value = draft,
                onValueChange = onDraft,
                modifier = Modifier.weight(1f).padding(horizontal = 8.dp),
                placeholder = { Text(if (listening) "在听…" else "告诉小助你要做什么…") },
                maxLines = 4,
                shape = RoundedCornerShape(12.dp),
            )

            Button(
                onClick = if (streaming) onStop else onSend,
                enabled = streaming || draft.isNotBlank() || pendingImages.isNotEmpty(),
                modifier = Modifier.padding(bottom = 4.dp),
                shape = RoundedCornerShape(12.dp),
                colors = androidx.compose.material3.ButtonDefaults.buttonColors(
                    // PC 版停止态是红渐变
                    containerColor = if (streaming) PlannerColors.Danger else PlannerColors.Primary,
                ),
            ) { Text(if (streaming) "停止" else "发送") }
        }
    }
}

/**
 * 拍照输出用的临时文件 URI（走 FileProvider）。
 *
 * 用 `TakePicture` 而不是 `TakePicturePreview`：后者只给一张缩略图，
 * 拍文档/白板时糊得看不清，模型也读不出内容。
 */
private fun newPhotoUri(context: android.content.Context): android.net.Uri {
    val dir = java.io.File(context.cacheDir, "photos").apply { mkdirs() }
    val file = java.io.File(dir, "shot-${System.currentTimeMillis()}.jpg")
    return androidx.core.content.FileProvider.getUriForFile(
        context,
        "${context.packageName}.fileprovider",
        file,
    )
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
    streaming: Boolean,
) {
    when (msg.kind) {
        UiMessageKind.System -> CenterNote(msg.text)
        UiMessageKind.Memory -> MemoryCard(msg)
        UiMessageKind.User -> UserRow(msg, onUndo)
        // 流式期间用纯文本（PC 版也是 `textContent +=`，等整段收束才渲染 markdown），
        // 否则半截的 `**加粗` 会把星号直接显示出来
        UiMessageKind.Assistant -> AssistantRow(msg, onSpeak, plain = streaming)
    }
}

/** 用户消息：右侧蓝色渐变气泡 + 撤销按钮（PC 版气泡下方右侧）。 */
@Composable
private fun UserRow(msg: UiMessage, onUndo: () -> Unit) {
    var confirming by remember { mutableStateOf(false) }

    // 两段确认（PC 版同款）：第一次点变成"确认撤销？"，3 秒内再点才执行，
    // 超时自动回退——不回退的话按钮会永久停在"确认撤销？"
    LaunchedEffect(confirming) {
        if (confirming) {
            kotlinx.coroutines.delay(3000)
            confirming = false
        }
    }

        // 用户消息：右侧蓝色渐变气泡 + 撤销按钮（PC 版气泡下方右侧）
        Column(
            modifier = Modifier.fillMaxWidth(),
            horizontalAlignment = Alignment.End,
        ) {
            // 图在气泡上方（自己的图看大一点，点开可放大）
            if (msg.images.isNotEmpty()) {
                ImageStrip(msg.images, modifier = Modifier.padding(bottom = 4.dp))
            }
            if (msg.text.isNotBlank()) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth(0.86f)
                        .clip(RoundedCornerShape(14.dp, 14.dp, 5.dp, 14.dp))
                        .background(PlannerColors.UserBubble)
                        .padding(horizontal = 13.dp, vertical = 9.dp),
                ) {
                    // 用户消息**不渲染 markdown**（PC 版是 textContent 原样）
                    Text(
                        msg.text,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Color.White,
                    )
                }
            }
        // PC 版只在"这条能撤销"时才给按钮（记忆卡片与历史回放没有 id）
        if (msg.rawIndex != null) {
            TextButton(
                onClick = {
                    if (confirming) {
                        confirming = false
                        onUndo()
                    } else {
                        confirming = true
                    }
                },
                contentPadding = PaddingValues(horizontal = 7.dp, vertical = 2.dp),
            ) {
                Text(
                    if (confirming) "确认撤销？" else "撤销",
                    style = MaterialTheme.typography.labelMedium,
                    color = if (confirming) Color.White else PlannerColors.TextSoft,
                    modifier = if (confirming) {
                        Modifier
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color(0xFFE5484D))
                            .padding(horizontal = 8.dp, vertical = 3.dp)
                    } else {
                        Modifier
                    },
                )
            }
        }
    }
}

/** 小助消息：头像 + 浅色气泡 + 工具卡片 + 朗读（PC 版气泡右侧一个圆形喇叭）。 */
@Composable
private fun AssistantRow(msg: UiMessage, onSpeak: (String) -> Unit, plain: Boolean) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            AssistantAvatar()
            Spacer(Modifier.width(6.dp))
            Text("小助", style = MaterialTheme.typography.labelMedium, color = PlannerColors.TextSoft)
        }
        Spacer(Modifier.height(4.dp))
        // 气泡与喇叭横排、底部对齐（PC 版 .msg-body）
        Row(verticalAlignment = Alignment.Bottom) {
            Box(
                modifier = Modifier
                    .weight(1f, fill = false)
                    .clip(RoundedCornerShape(14.dp, 14.dp, 14.dp, 5.dp))
                    .background(if (msg.failed) Color(0xFFFFECEF) else PlannerColors.Surface)
                    .border(1.dp, Color(0x22000000), RoundedCornerShape(14.dp, 14.dp, 14.dp, 5.dp))
                    .padding(horizontal = 13.dp, vertical = 9.dp),
            ) {
                when {
                    msg.text.isBlank() && msg.tools.isEmpty() -> Text(
                        "…",
                        style = MaterialTheme.typography.bodyMedium,
                        color = PlannerColors.TextSoft,
                    )
                    plain || msg.failed -> Text(
                        msg.text,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (msg.failed) PlannerColors.Danger else PlannerColors.Text,
                    )
                    else -> Text(
                        markdownText(msg.text),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
            if (msg.text.isNotBlank() && !msg.failed) {
                Spacer(Modifier.width(3.dp))
                Box(
                    modifier = Modifier
                        .size(22.dp)
                        .clip(CircleShape)
                        .clickable { onSpeak(msg.text) },
                    contentAlignment = Alignment.Center,
                ) {
                    Text("🔊", style = MaterialTheme.typography.labelMedium)
                }
            }
        }
        msg.tools.forEach { ToolCard(it) }
    }
}

/** 记忆卡片：PC 版 `.msg.memory` 的虚线框 + 固定小标题，内容走 markdown。 */
@Composable
private fun MemoryCard(msg: UiMessage) {
    Column(modifier = Modifier.fillMaxWidth()) {
        msg.memoryMeta?.let {
            Text(
                it,
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.Primary,
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
                markdownText(msg.text, baseColor = PlannerColors.TextSoft),
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
private fun ToolCard(tool: UiTool) {
    // PC 版：运行中显示「正在执行 X」+ 转圈；完成显示 ✓ + 名字；
    // 点卡片展开/收起参数与结果（历史卡片直接是完成态，不转圈）
    var expanded by remember { mutableStateOf(false) }
    val running = tool.live && tool.result == null

    Card(
        modifier = Modifier
            .fillMaxWidth(0.92f)
            .padding(top = 6.dp)
            .clickable { expanded = !expanded },
        colors = CardDefaults.cardColors(containerColor = Color(0xFFF4F7FF)),
        border = androidx.compose.foundation.BorderStroke(
            1.dp,
            if (running) PlannerColors.Primary else Color(0x593DBF8F),
        ),
    ) {
        Column(modifier = Modifier.padding(horizontal = 11.dp, vertical = 7.dp)) {
            Text(
                (if (running) "正在执行 " else "✓ ") +
                    com.xiaozhu.planner.core.text.Markdown.toolDisplayName(tool.name),
                style = MaterialTheme.typography.labelMedium,
                color = if (running) PlannerColors.Primary else PlannerColors.Ok,
            )
            if (expanded && tool.args.isNotBlank()) {
                Text(
                    tool.args,
                    style = MaterialTheme.typography.bodySmall,
                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            Text(
                tool.result ?: "…",
                style = MaterialTheme.typography.bodySmall,
                color = PlannerColors.TextSoft,
                maxLines = if (expanded) 40 else 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(top = 4.dp),
            )
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
