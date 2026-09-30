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
import androidx.compose.foundation.layout.heightIn
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
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
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

    // 相册：用系统照片选择器（不需要读存储权限）
    val gallery = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri -> if (uri != null) vm.attachImage(uri) }

    // 拍照：拍完写进我们给的临时文件，从那里读全分辨率原图
    var photoUri by remember { mutableStateOf<android.net.Uri?>(null) }
    val camera = rememberLauncherForActivityResult(
        // 用自定义契约：必须把写权限授给相机 App，否则它写不进文件（见该类的注释）
        TakePictureWithPermission()
    ) { ok ->
        val uri = photoUri
        photoUri = null
        if (ok && uri != null) vm.attachImage(uri) else if (!ok) vm.showHint("没有拍照。")
    }

    // 相机权限：先申请再启动（部分 ROM 的相机 App 会检查调用方是否持有 CAMERA，
    // 没有就直接拒绝——这是"点了拍照没反应/拍完读不出来"的另一条路径）
    val cameraPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) launchCamera(context) { photoUri = it; camera.launch(it) }
        else vm.showHint("没有相机权限，拍不了照。可以先用「从相册选」，或在系统设置里给相机权限。")
    }

    // 本地文档：SAF 打开（拿到的是可读 URI，不需要存储权限）
    val document = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> if (uri != null) vm.attachDocument(uri, displayNameOf(context, uri)) }

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
                    streaming = state.streaming,
                    pendingImages = state.pendingImages,
                    pendingDocName = state.pendingDocName,
                    onPickImage = {
                        // 系统相册选择器（Android 13+ 是照片选择器，不需要读存储权限）
                        gallery.launch(
                            androidx.activity.result.PickVisualMediaRequest(
                                ActivityResultContracts.PickVisualMedia.ImageOnly
                            )
                        )
                    },
                    onTakePhoto = {
                        // 先确认相机权限，再启动相机（见 cameraPermission 的注释）
                        if (ContextCompat.checkSelfPermission(
                                context, Manifest.permission.CAMERA
                            ) == PackageManager.PERMISSION_GRANTED
                        ) {
                            launchCamera(context) { photoUri = it; camera.launch(it) }
                        } else {
                            cameraPermission.launch(Manifest.permission.CAMERA)
                        }
                    },
                    onRemoveImage = { vm.removePendingImage(it) },
                    onPickDocument = {
                        // SAF：任何能提供文档的 App 都行（文件管理器、网盘、下载…），
                        // 不需要存储权限
                        document.launch(arrayOf(
                            "text/*",
                            "application/pdf",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            "application/json",
                            "application/octet-stream",
                        ))
                    },
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

/**
 * 标题栏那一行：**只显示当前上下文大概多少 token**。
 *
 * 之前这里还堆了"记忆 N 个节点 · 对话 M 条"和心跳倒计时——信息多但并不都需要
 * 一直看着，而 token 数才是"它还记得多少"的直接指标（也直接关系到花钱）。
 * 记忆概览移到设置页与记忆浏览页；心跳倒计时留在需要它的地方（日历页签的逾期角标照旧）。
 */
private fun statusLine(state: ChatUiState): String =
    if (state.contextTokens > 0) "上下文约 ${state.contextTokens} token" else "还没有上下文"

@Composable
private fun EmptyState(onOpenImport: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 40.dp, start = 24.dp, end = 24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        AssistantAvatar(size = 56.dp)
        Text(
            "我是小助",
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(top = 12.dp),
        )
        Text(
            "安排日程、提醒你做事、记住你说过的话。\n直接说就行，比如「明天下午三点提醒我改论文」。",
            style = MaterialTheme.typography.bodyMedium,
            color = PlannerColors.TextSoft,
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            modifier = Modifier.padding(top = 8.dp),
        )
        TextButton(
            onClick = onOpenImport,
            modifier = Modifier.padding(top = 12.dp),
        ) { Text("导入电脑上的记忆") }
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
 * 输入区。
 *
 * 版式：整条输入区是一块圆角"托盘"（浅色卡片 + 细边），里面是输入框与按钮；
 * ＋ 与发送都是**圆形**按钮，配色与气泡一致。
 * 附件的三个入口用**底部弹层**（图标 + 标题 + 说明的三行），而不是系统默认的
 * 那个下拉菜单——后者是 Android 4.x 的观感，跟这套界面不搭。
 */
@Composable
private fun InputBar(
    draft: String,
    onDraft: (String) -> Unit,
    streaming: Boolean,
    pendingImages: List<String>,
    pendingDocName: String?,
    onPickImage: () -> Unit,
    onTakePhoto: () -> Unit,
    onPickDocument: () -> Unit,
    onRemoveImage: (Int) -> Unit,
    onSend: () -> Unit,
    onStop: () -> Unit,
) {
    var sheet by remember { mutableStateOf(false) }
    val canSend = draft.isNotBlank() || pendingImages.isNotEmpty()

    if (sheet) {
        AttachSheet(
            onDismiss = { sheet = false },
            onTakePhoto = { sheet = false; onTakePhoto() },
            onPickImage = { sheet = false; onPickImage() },
            onPickDocument = { sheet = false; onPickDocument() },
        )
    }

    Surface(
        color = PlannerColors.Surface,
        shape = RoundedCornerShape(topStart = 18.dp, topEnd = 18.dp),
        shadowElevation = 8.dp,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(horizontal = 10.dp, vertical = 8.dp)) {
            // 已附加的文档（正文在发送时并进消息，这里只报个名字）
            pendingDocName?.let {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(bottom = 6.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .background(PlannerColors.MemoryCard)
                        .padding(horizontal = 10.dp, vertical = 6.dp),
                ) {
                    Text("📄", style = MaterialTheme.typography.labelLarge)
                    Text(
                        it,
                        style = MaterialTheme.typography.labelMedium,
                        color = PlannerColors.Primary,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.padding(start = 6.dp),
                    )
                }
            }
            if (pendingImages.isNotEmpty()) {
                PendingImageStrip(pendingImages, onRemove = onRemoveImage)
            }

            Row(verticalAlignment = Alignment.Bottom) {
                // ＋：圆形、浅蓝底，和发送按钮同尺寸
                CircleAction(
                    glyph = "＋",
                    background = PlannerColors.MemoryCard,
                    tint = PlannerColors.Primary,
                    onClick = { sheet = true },
                )
                OutlinedTextField(
                    value = draft,
                    onValueChange = onDraft,
                    modifier = Modifier
                        .weight(1f)
                        .padding(horizontal = 8.dp)
                        // 固定最小高度：提示文字长短不该影响输入框大小
                        .heightIn(min = 48.dp),
                    placeholder = {
                        Text(
                            "说点什么…",
                            maxLines = 1,
                            softWrap = false,
                            overflow = TextOverflow.Ellipsis,
                        )
                    },
                    maxLines = 4,
                    shape = RoundedCornerShape(14.dp),
                )
                CircleAction(
                    glyph = if (streaming) "■" else "↑",
                    background = if (streaming) PlannerColors.Danger
                    else if (canSend) PlannerColors.Primary else Color(0xFFD8DEE9),
                    tint = Color.White,
                    enabled = streaming || canSend,
                    onClick = { if (streaming) onStop() else onSend() },
                )
            }
        }
    }
}

/** 圆形操作按钮（＋ / 发送 / 停止）：颜色表达状态，禁用时灰掉。 */
@Composable
private fun CircleAction(
    glyph: String,
    background: Color,
    tint: Color,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    Box(
        modifier = Modifier
            .padding(bottom = 4.dp)
            .size(42.dp)
            .clip(CircleShape)
            .background(background)
            .clickable(enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            glyph,
            style = MaterialTheme.typography.titleMedium,
            color = tint,
        )
    }
}

/** 附件入口的底部弹层：三行，每行一个图标 + 标题 + 说明。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AttachSheet(
    onDismiss: () -> Unit,
    onTakePhoto: () -> Unit,
    onPickImage: () -> Unit,
    onPickDocument: () -> Unit,
) {
    ModalBottomSheet(
        onDismissRequest = onDismiss,
        containerColor = PlannerColors.Surface,
    ) {
        Text(
            "添加内容",
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(start = 20.dp, bottom = 4.dp),
        )
        AttachRow(
            glyph = "📷",
            tint = Color(0xFF5B7CFA),
            title = "拍张照",
            subtitle = "拍纸上的东西、白板、书页，它能直接看",
            onClick = onTakePhoto,
        )
        AttachRow(
            glyph = "🖼",
            tint = Color(0xFF39C5BB),
            title = "从相册选",
            subtitle = "选一张已有的图片",
            onClick = onPickImage,
        )
        AttachRow(
            glyph = "📄",
            tint = Color(0xFFF2A03D),
            title = "本地文档",
            subtitle = "txt / md / docx 读正文，pdf 按页看",
            onClick = onPickDocument,
        )
        Spacer(Modifier.height(12.dp))
    }
}

@Composable
private fun AttachRow(
    glyph: String,
    tint: Color,
    title: String,
    subtitle: String,
    onClick: () -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 20.dp, vertical = 10.dp),
    ) {
        Box(
            modifier = Modifier
                .size(40.dp)
                .clip(CircleShape)
                .background(tint.copy(alpha = 0.12f)),
            contentAlignment = Alignment.Center,
        ) {
            Text(glyph, style = MaterialTheme.typography.titleMedium)
        }
        Column(modifier = Modifier.padding(start = 14.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = PlannerColors.TextSoft,
            )
        }
    }
}

/**
 * 启动相机：先建好临时文件 URI，再交给相机 App 写。
 *
 * 建不出 URI（FileProvider 配错、cache 满了）时给一句人话，而不是静默什么都不发生。
 */
private fun launchCamera(context: android.content.Context, onReady: (android.net.Uri) -> Unit) {
    val uri = runCatching { newPhotoUri(context) }.getOrNull()
    if (uri == null) {
        android.widget.Toast.makeText(context, "没法创建拍照用的临时文件", android.widget.Toast.LENGTH_SHORT).show()
    } else {
        onReady(uri)
    }
}

/**
 * 从 SAF 的 URI 里问出显示名（"论文终稿.pdf"这种）。
 *
 * 取不到就退回路径末段——显示名只用于界面提示与附件标题，
 * 取不到不该让整个选择失败。
 */
private fun displayNameOf(context: android.content.Context, uri: android.net.Uri): String? =
    runCatching {
        context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            if (index >= 0 && cursor.moveToFirst()) cursor.getString(index) else null
        }
    }.getOrNull() ?: uri.lastPathSegment?.substringAfterLast('/')

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

/**
 * 让气泡里的文字**可以长按选中**，选完用系统菜单复制。
 *
 * 对应 PC 版的"选中后右键复制"（PC 只对 `.bubble` / `code` / `.tool-pre` 开了
 * `user-select: text`，其余地方禁止选择——这里是同一套范围）。
 *
 * `SelectionContainer` 会给出原生选择手柄，所以"选多少"由用户拖手柄决定，
 * 系统菜单里也有全选；比自己实现一套选区靠谱得多。
 */
@Composable
private fun Selectable(content: @Composable () -> Unit) {
    androidx.compose.foundation.text.selection.SelectionContainer {
        content()
    }
}

/** 复制到剪贴板并弹一句"已复制"（与 PC 版那个 1.2 秒的提示同义）。 */
private fun copyToClipboard(context: android.content.Context, text: String) {
    if (text.isBlank()) return
    runCatching {
        val clipboard = context.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
            as android.content.ClipboardManager
        clipboard.setPrimaryClip(android.content.ClipData.newPlainText("小助", text))
        android.widget.Toast.makeText(context, "已复制", android.widget.Toast.LENGTH_SHORT).show()
    }.onFailure {
        android.widget.Toast.makeText(context, "复制失败", android.widget.Toast.LENGTH_SHORT).show()
    }
}

/** 小图标按钮（复制 / 朗读共用）：22dp 圆形，点了有反馈。 */
@Composable
private fun BubbleAction(glyph: String, description: String, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .size(22.dp)
            .clip(CircleShape)
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(glyph, style = MaterialTheme.typography.labelMedium)
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
    streaming: Boolean,
) {
    when (msg.kind) {
        UiMessageKind.System -> CenterNote(msg.text)
        UiMessageKind.Memory -> MemoryCard(msg)
        UiMessageKind.User -> UserRow(msg, onUndo)
        // 流式期间用纯文本（PC 版也是 `textContent +=`，等整段收束才渲染 markdown），
        // 否则半截的 `**加粗` 会把星号直接显示出来
        UiMessageKind.Assistant -> AssistantRow(msg, onSpeak, plain = streaming, streaming = streaming)
    }
}

/** 用户消息：右侧蓝色渐变气泡 + 撤销按钮（PC 版气泡下方右侧）。 */
@Composable
private fun UserRow(msg: UiMessage, onUndo: () -> Unit) {
    var confirming by remember { mutableStateOf(false) }
    val context = LocalContext.current

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
            // 长按气泡也能选中复制；这里再给一个"复制整条"的快捷入口
            if (msg.text.isNotBlank()) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    BubbleAction(glyph = "📋", description = "复制这条") {
                        copyToClipboard(context, msg.text)
                    }
                    Spacer(Modifier.width(4.dp))
                    Text(
                        "复制",
                        style = MaterialTheme.typography.labelMedium,
                        color = PlannerColors.TextSoft,
                        modifier = Modifier.clickable { copyToClipboard(context, msg.text) },
                    )
                }
            }
            if (msg.text.isNotBlank()) {
                // 靠右 + **贴着文字宽度**
                //
                // 之前是 `fillMaxWidth(0.86f)`：不管文字多短，气泡都占满 86% 宽度，
                // 于是短消息右边拖着一大片蓝色空白，看着像左对齐的宽条。
                // 正确写法是放在 Arrangement.End 的 Row 里用 weight(fill = false)——
                // 最多占满可用宽度、内容短就贴合内容，同时保持右对齐。
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End,
                ) {
                    Box(
                        modifier = Modifier
                            .weight(1f, fill = false)
                            .clip(RoundedCornerShape(14.dp, 14.dp, 5.dp, 14.dp))
                            .background(PlannerColors.UserBubble)
                            .padding(horizontal = 13.dp, vertical = 9.dp),
                    ) {
                        // 可长按选中复制（PC 版同款：只对气泡正文开选择）
                        Selectable {
                            // 用户消息**不渲染 markdown**（PC 版是 textContent 原样）
                            Text(
                                msg.text,
                                style = MaterialTheme.typography.bodyMedium,
                                color = Color.White,
                            )
                        }
                    }
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
private fun AssistantRow(msg: UiMessage, onSpeak: (String) -> Unit, plain: Boolean, streaming: Boolean) {
    val context = LocalContext.current
    // 只调了工具、没有说话：**不画空气泡**，只留工具卡片（PC 版就是这样）。
    // 之前会留一个"…"或空白气泡，看起来像它说了句空话。
    val hasText = msg.text.isNotBlank()
    if (!hasText && !streaming && msg.tools.isEmpty()) return

    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            AssistantAvatar()
            Spacer(Modifier.width(6.dp))
            Text("小助", style = MaterialTheme.typography.labelMedium, color = PlannerColors.TextSoft)
        }
        Spacer(Modifier.height(4.dp))
        // 气泡与喇叭横排、底部对齐（PC 版 .msg-body）
        if (hasText || streaming) {
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
                        // 还没吐字、也没有卡片：这一轮刚开始，先占个位
                        !hasText -> Text(
                            "…",
                            style = MaterialTheme.typography.bodyMedium,
                            color = PlannerColors.TextSoft,
                        )
                        plain || msg.failed -> Selectable {
                            Text(
                                msg.text,
                                style = MaterialTheme.typography.bodyMedium,
                                color = if (msg.failed) PlannerColors.Danger else PlannerColors.Text,
                            )
                        }
                        else -> Selectable {
                            Text(
                                markdownText(msg.text),
                                style = MaterialTheme.typography.bodyMedium,
                            )
                        }
                    }
                }
                if (hasText && !msg.failed) {
                    Spacer(Modifier.width(3.dp))
                    Column {
                        // 复制整条（手机上拖手柄选一段不如直接复制方便，两个都给）
                        BubbleAction(glyph = "📋", description = "复制这条") {
                            copyToClipboard(context, msg.text)
                        }
                        Spacer(Modifier.height(2.dp))
                        BubbleAction(glyph = "🔊", description = "朗读这条") {
                            onSpeak(msg.text)
                        }
                    }
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
            Selectable {
                Text(
                    markdownText(msg.text, baseColor = PlannerColors.TextSoft),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
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
                Selectable {
                    Text(
                        tool.args,
                        style = MaterialTheme.typography.bodySmall,
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                        color = PlannerColors.TextSoft,
                        modifier = Modifier.padding(top = 4.dp),
                    )
                }
            }
            Selectable {
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
/** 记忆概览文案（设置页 / 记忆浏览页用；标题栏只显示 token）。 */
private fun memoryLine(stats: MemoryStats): String = when {
    stats.totalNodes == 0 && stats.rawMessages == 0 -> "还没有记忆"
    stats.totalNodes == 0 -> "对话 ${stats.rawMessages} 条"
    else -> "记忆 ${stats.totalNodes} 个节点 · 对话 ${stats.rawMessages} 条"
}

@Suppress("unused")
private val unusedWeight = FontWeight.Medium
