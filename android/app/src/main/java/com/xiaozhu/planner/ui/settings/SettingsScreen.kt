package com.xiaozhu.planner.ui.settings

import android.app.Application
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.xiaozhu.planner.core.llm.ApiKeyCheck
import com.xiaozhu.planner.core.llm.EndpointUrl
import com.xiaozhu.planner.core.llm.ThinkingControl
import com.xiaozhu.planner.data.SettingsStore
import com.xiaozhu.planner.reminder.ReminderNotifications
import com.xiaozhu.planner.ui.theme.PlannerColors

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    onBack: () -> Unit,
    incomingFile: android.net.Uri? = null,
    onFileConsumed: () -> Unit = {},
    /** 打开图片管理页（图片占用超上限时的"去清理"也走这里）。 */
    onOpenImages: () -> Unit = {},
) {
    val app = LocalContext.current.applicationContext as Application
    val store = remember { SettingsStore(app) }

    var apiKey by remember { mutableStateOf(store.apiKey) }
    var baseUrl by remember { mutableStateOf(store.baseUrl) }
    var model by remember { mutableStateOf(store.model) }
    var ttsEnabled by remember { mutableStateOf(store.ttsEnabled) }
    var ttsEngine by remember { mutableStateOf(store.ttsEngine) }
    var ttsVoice by remember { mutableStateOf(store.ttsVoice) }
    var mimoApiKey by remember { mutableStateOf(store.mimoApiKey) }
    var ocSession by remember { mutableStateOf(store.openCodeSession) }
    var showKey by remember { mutableStateOf(false) }
    var saved by remember { mutableStateOf(false) }
    // 思考开关与强度（点选后立即生效，和端点设置一样不需要点保存）
    var thinkingMode by remember { mutableStateOf(store.thinkingMode) }
    var effortValue by remember { mutableStateOf(store.reasoningEffort) }
    // 收起文字工具条要用（点空白处调一次）
    val focusManager = androidx.compose.ui.platform.LocalFocusManager.current
    val textToolbar = androidx.compose.ui.platform.LocalTextToolbar.current

    Scaffold(
        // 点空白处收起粘贴/复制那个悬浮条（设置页也有好几个输入框）
        modifier = Modifier.pointerInput(Unit) {
            // detectTapGestures 是 PointerInputScope 的扩展，必须直接调（不能写全限定名）
            detectTapGestures(
                onTap = {
                    focusManager.clearFocus(force = true)
                    textToolbar.hide()
                },
            )
        },
        topBar = {
            TopAppBar(
                title = { Text("设置") },
                actions = { TextButton(onClick = onBack) { Text("返回") } },
            )
        }
    ) { pad ->
        Column(
            modifier = Modifier
                .padding(pad)
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Text("模型服务", style = MaterialTheme.typography.titleMedium)            // 把"现在到底在用哪个端点"摆出来：只显示输入框的话，用户看不出默认值是否生效
            Text(
                "当前在用：${store.baseUrl}　模型 ${store.model}" +
                    if (store.usingDefaultEndpoint()) "（默认）" else "（你改过）",
                style = MaterialTheme.typography.bodySmall,
                color = if (store.usingDefaultEndpoint()) PlannerColors.TextSoft else PlannerColors.Primary,
                modifier = Modifier.padding(top = 4.dp),
            )
            Text(
                "密钥用 Android 的加密存储保存，只留在本机，不上传。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 4.dp),
            )
            // 一键还原：不用去猜"默认到底填什么"
            TextButton(onClick = {
                store.resetEndpointToDefaults()
                baseUrl = store.baseUrl
                model = store.model
                saved = true
            }) { Text("恢复默认端点（DeepSeek 官方 + deepseek-flash）") }

            OutlinedTextField(
                value = apiKey,
                onValueChange = { apiKey = it; saved = false },
                label = { Text("API Key") },
                // 密码态看不到内容，粘贴带进中文/说明文字时完全没法自查 → 给个"显示"开关
                visualTransformation = if (showKey) androidx.compose.ui.text.input.VisualTransformation.None
                else PasswordVisualTransformation(),
                trailingIcon = {
                    TextButton(onClick = { showKey = !showKey }) {
                        Text(if (showKey) "隐藏" else "显示", style = MaterialTheme.typography.labelMedium)
                    }
                },
                singleLine = true,
                isError = ApiKeyCheck.problem(apiKey) != null,
                supportingText = {
                    // 即时体检：密钥里有中文/空格/多余前缀时立刻说清楚
                    ApiKeyCheck.problem(apiKey)?.let {
                        Text(it, color = MaterialTheme.colorScheme.error)
                    } ?: Text("只填密钥本身（前缀 Bearer 由程序加）")
                },
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = baseUrl,
                onValueChange = { baseUrl = it; saved = false },
                label = { Text("Base URL") },
                singleLine = true,
                supportingText = {
                    Text(
                        "默认 DeepSeek 官方：${SettingsStore.DEFAULT_BASE}\n" +
                            "带不带 /chat/completions 都行（会自动去掉重复的一段）。" +
                            "换 opencode 网关：${SettingsStore.OPENCODE_BASE} + deepseek-v4.1-flash"
                    )
                },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
            OutlinedTextField(
                value = model,
                onValueChange = { model = it; saved = false },
                label = { Text("模型") },
                singleLine = true,
                supportingText = {
                    Text(
                        "例如 ${SettingsStore.DEFAULT_MODEL}" +
                            "（DeepSeek 官方；它的正式名字是 DeepSeek-V4.1-Flash）。" +
                            "官方只认 deepseek-flash / deepseek-v4-pro"
                    )
                },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )

            // opencode 网关额外要的会话头：平时不用管（自动生成一个并记住），
            // 只有网关改了要求时才需要手填
            if (baseUrl.contains("opencode.ai", ignoreCase = true)) {
                OutlinedTextField(
                    value = ocSession,
                    onValueChange = { ocSession = it; saved = false },
                    label = { Text("opencode session（一般不用改）") },
                    singleLine = true,
                    supportingText = {
                        Text("这个网关要求 ${EndpointUrl.SESSION_HEADER} 头，已自动生成；清空会重新生成一个")
                    },
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                )
            }

            Button(
                onClick = {
                    store.apiKey = apiKey
                    store.baseUrl = baseUrl
                    store.model = model
                    store.ttsEnabled = ttsEnabled
                    store.ttsEngine = ttsEngine
                    store.ttsVoice = ttsVoice
                    store.mimoApiKey = mimoApiKey
                    store.openCodeSession = ocSession
                    saved = true
                },
                modifier = Modifier.padding(top = 16.dp),
            ) { Text("保存") }

            // ── 测试连接 ────────────────────────────────────────
            val checkVm: ConnectionCheckViewModel = viewModel()
            val checkState by checkVm.state.collectAsState()
            Row(verticalAlignment = Alignment.CenterVertically) {
                TextButton(
                    onClick = { checkVm.run() },
                    enabled = !checkState.running,
                    modifier = Modifier.padding(start = 8.dp),
                ) { Text(if (checkState.running) "测试中…" else "测试连接") }
                Text(
                    "先保存再测（用的是已保存的值）",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            checkState.detail?.let { detail ->
                Text(
                    detail,
                    style = MaterialTheme.typography.bodySmall,
                    color = if (checkState.ok == true) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }

            if (saved) {
                Text(
                    "已保存。",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }

            // ── 语音 ────────────────────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("语音", style = MaterialTheme.typography.titleMedium)
            Text(
                "回复会被朗读。默认走小米 MiMo（与电脑版同一把嗓子），" +
                    "没配密钥或请求失败时退回系统语音——有声音总比没有好。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("自动朗读回复", modifier = Modifier.weight(1f))
                Switch(checked = ttsEnabled, onCheckedChange = { ttsEnabled = it; saved = false })
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("只用系统语音", modifier = Modifier.weight(1f))
                Switch(
                    checked = ttsEngine == SettingsStore.TTS_ENGINE_SYSTEM,
                    onCheckedChange = {
                        ttsEngine = if (it) SettingsStore.TTS_ENGINE_SYSTEM else SettingsStore.TTS_ENGINE_MIMO
                        saved = false
                    },
                )
            }

            OutlinedTextField(
                value = mimoApiKey,
                onValueChange = { mimoApiKey = it; saved = false },
                label = { Text("MiMo API Key（可留空）") },
                visualTransformation = PasswordVisualTransformation(),
                singleLine = true,
                supportingText = { Text("与上面 LLM 的 key 不是同一个供应商，不能混用") },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
            OutlinedTextField(
                value = ttsVoice,
                onValueChange = { ttsVoice = it; saved = false },
                label = { Text("音色") },
                singleLine = true,
                supportingText = {
                    Text("如 ${SettingsStore.MIMO_DEFAULT_VOICE} / 冰糖 / 茉莉 / 苏打 / 白桦")
                },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )

            Text(
                "语音输入用系统识别（零密钥、多数机型可离线）。识别不准就打字，不影响用。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 8.dp),
            )

            // ── 思考（开关 + 强度）──────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("思考", style = MaterialTheme.typography.titleMedium)
            Text(
                "回答前先想一遍会让复杂问题答得更好，简单问题则纯属多花时间。" +
                    "自适应就是让它自己判断。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 6.dp),
            )

            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                ThinkingControl.MODES.forEach { mode ->
                    FilterChip(
                        selected = thinkingMode == mode,
                        onClick = { store.thinkingMode = mode; thinkingMode = mode },
                        label = { Text(ThinkingControl.modeLabel(mode)) },
                    )
                }
            }
            Text(
                ThinkingControl.modeHint(thinkingMode),
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
                modifier = Modifier.padding(top = 2.dp),
            )

            // 关闭思考时强度不生效（官方会忽略这个字段），所以藏起来——
            // 摆一个不起作用的控件比不摆更糟
            if (thinkingMode != ThinkingControl.DISABLED) {
                Text(
                    "想多久",
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(top = 10.dp),
                )
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState())
                        .padding(top = 4.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    ThinkingControl.EFFORTS.forEach { effort ->
                        FilterChip(
                            selected = effortValue == effort,
                            onClick = { store.reasoningEffort = effort; effortValue = effort },
                            label = { Text(ThinkingControl.effortLabel(effort)) },
                        )
                    }
                }
                Text(
                    "档位差别在难题上才明显，简单问题三档几乎一样。",
                    style = MaterialTheme.typography.labelMedium,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(top = 2.dp),
                )
            } else {
                Text(
                    "已关闭思考：最快，但复杂问题会明显变差。",
                    style = MaterialTheme.typography.labelMedium,
                    color = PlannerColors.Danger,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }

            // ── 记忆压缩（与 PC 版同名同范围同交叉约束）──────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("记忆压缩", style = MaterialTheme.typography.titleMedium)
            Text(
                "对话攒到一定条数，最早的会被总结成一个记忆节点，逐层向上合并成记忆树。" +
                    "四个参数和电脑上那套一样；改完立即生效，不用重启。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )

            var triggerText by remember { mutableStateOf(store.compressTrigger.toString()) }
            var keepText by remember { mutableStateOf(store.compressKeep.toString()) }
            var thresholdText by remember { mutableStateOf(store.compactThreshold.toString()) }
            var factorText by remember { mutableStateOf(store.compactFactor.toString()) }

            NumberField(
                label = "触发条数（原始消息到这么多条就压缩）",
                hint = "20–500，默认 60",
                value = triggerText,
                onValue = { triggerText = it },
            )
            NumberField(
                label = "保留条数（最近这么多条永远留在上下文）",
                hint = "5–200，默认 20，必须小于触发条数",
                value = keepText,
                onValue = { keepText = it },
            )
            NumberField(
                label = "合并阈值（某层节点到这么多就往上并）",
                hint = "3–50，默认 8",
                value = thresholdText,
                onValue = { thresholdText = it },
            )
            NumberField(
                label = "合并个数（每次并几个节点）",
                hint = "2–10，默认 4，必须小于合并阈值",
                value = factorText,
                onValue = { factorText = it },
            )

            // 参数不合法就说清楚（PC 版是在示意图上画一行红字，同样不拦保存）
            store.compressionProblem()?.let {
                Text(
                    "参数不合法：$it",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }

            Row(modifier = Modifier.padding(top = 6.dp)) {
                Button(
                    onClick = {
                        store.compressTrigger = triggerText.toIntOrNull() ?: store.compressTrigger
                        store.compressKeep = keepText.toIntOrNull() ?: store.compressKeep
                        store.compactThreshold = thresholdText.toIntOrNull() ?: store.compactThreshold
                        store.compactFactor = factorText.toIntOrNull() ?: store.compactFactor
                        // 夹紧之后再回显，用户能看到实际存下来的值
                        triggerText = store.compressTrigger.toString()
                        keepText = store.compressKeep.toString()
                        thresholdText = store.compactThreshold.toString()
                        factorText = store.compactFactor.toString()
                        saved = true
                    },
                ) { Text("保存压缩参数") }
                TextButton(
                    onClick = {
                        store.compressTrigger = 60
                        store.compressKeep = 20
                        store.compactThreshold = 8
                        store.compactFactor = 4
                        triggerText = "60"; keepText = "20"; thresholdText = "8"; factorText = "4"
                    },
                    modifier = Modifier.padding(start = 8.dp),
                ) { Text("恢复默认") }
            }

            // ── 提醒权限 ────────────────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("提醒权限", style = MaterialTheme.typography.titleMedium)
            Text(
                "日历到点提醒靠通知 + 精确闹钟。任一项没给，" +
                    "“晚上 8 点提醒我”就会要么不响、要么晚几分钟——" +
                    "而它仍然会说自己提醒了，所以这里要看一眼。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )

            val context = LocalContext.current
            val alarmManager = remember {
                context.getSystemService(android.content.Context.ALARM_SERVICE) as? android.app.AlarmManager
            }
            val notificationsOk = ReminderNotifications.canPost(context)
            val exactOk = Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
                (alarmManager?.canScheduleExactAlarms() ?: false)

            PermissionRow("通知", notificationsOk) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    context.startActivity(
                        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                }
            }
            PermissionRow("精确闹钟", exactOk) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    context.startActivity(
                        Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM)
                            .setData(android.net.Uri.parse("package:${context.packageName}"))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                }
            }
            Text(
                "没拿到精确闹钟时会退化成「大概这个点」，不精确但不会丢。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            // ── 图片（占用与清理）──────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("图片", style = MaterialTheme.typography.titleMedium)
            ImageStorageRow(onOpenImages = onOpenImages)

            // ── 从电脑导入旧数据 ─────────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("导入旧数据", style = MaterialTheme.typography.titleMedium)
            Text(
                "把电脑上那份记忆与日历搬到手机（一次性）。先在电脑上跑：" +
                    "scripts/export_to_android.py → 把生成的 JSON 传进手机 → 这里选它。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )

            val importVm: ImportViewModel = viewModel()
            val importState by importVm.state.collectAsState()
            val picker = rememberLauncherForActivityResult(
                ActivityResultContracts.OpenDocument()
            ) { uri ->
                if (uri != null) {
                    importVm.load { context.contentResolver.openInputStream(uri) }
                }
            }

            // 从外面点开/分享进来的文件：进来就开始读，用户只要确认
            LaunchedEffect(incomingFile) {
                incomingFile?.let { uri ->
                    importVm.load { context.contentResolver.openInputStream(uri) }
                    onFileConsumed()
                }
            }

            Button(
                onClick = { picker.launch(arrayOf("application/json", "application/gzip", "*/*")) },
                enabled = !importState.busy,
            ) { Text(if (importState.busy) "处理中…" else "选择导出文件") }

            // 备份：导出的格式与 PC 的导出脚本一致，两边能互相导
            val exporter = rememberLauncherForActivityResult(
                ActivityResultContracts.CreateDocument("application/json")
            ) { uri -> if (uri != null) importVm.exportTo(uri) }
            TextButton(
                onClick = {
                    val stamp = java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd")
                        .format(java.time.LocalDate.now())
                    exporter.launch("planner-backup-$stamp.json")
                },
                enabled = !importState.busy,
            ) { Text("导出备份（记忆 + 对话 + 日历）") }

            // 随包带了旧数据时（"给自己装机"的构建）：一键导入，不用传文件
            if (remember { importVm.hasBundledSeed() }) {
                Text(
                    "这个安装包里**已经带了**电脑上的那份旧数据。",
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 8.dp),
                )
                Row {
                    Button(
                        onClick = { importVm.importBundled(wipeFirst = true) },
                        enabled = !importState.busy,
                    ) { Text("导入并替换手机上的") }
                    TextButton(
                        onClick = { importVm.importBundled(wipeFirst = false) },
                        enabled = !importState.busy,
                        modifier = Modifier.padding(start = 8.dp),
                    ) { Text("只补空的") }
                }
                Text(
                    "「导入并替换」会先清空手机上现有的对话与日程（首次装机选它）。",
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                )
            }

            importState.error?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
            importState.message?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }

            importState.preview?.let { p ->
                Card(modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text("导出文件里有", style = MaterialTheme.typography.titleSmall)
                        Text(
                            "记忆节点 ${p.nodes} 个（含原文 ${p.leavesWithDetails} 个）、" +
                                "对话 ${p.bufferMessages} 条、日程 ${p.calendarItems} 项",
                            style = MaterialTheme.typography.bodySmall,
                        )
                        p.exportedAt?.let {
                            Text("导出时间：$it", style = MaterialTheme.typography.bodySmall)
                        }
                        p.source?.let {
                            Text("来源：$it", style = MaterialTheme.typography.bodySmall)
                        }
                        if (p.warnings.isNotEmpty()) {
                            Text(
                                "导出时的提醒：" + p.warnings.take(3).joinToString("；"),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.error,
                            )
                        }

                        if (p.hasLocalData) {
                            Text(
                                "手机上已有：记忆节点 ${p.localNodes} 个、对话 ${p.localMessages} 条、" +
                                    "日程 ${p.localCalendar} 项。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.error,
                                modifier = Modifier.padding(top = 6.dp),
                            )
                            Text(
                                "「清空后导入」会先删掉这些再写入——第一次装 App 时选它；" +
                                    "已经在手机上聊过一阵子的话，选「追加」更安全（可能有重复）。",
                                style = MaterialTheme.typography.bodySmall,
                            )
                            Row {
                                Button(
                                    onClick = { importVm.confirm(wipeFirst = true) },
                                    enabled = !importState.busy,
                                ) { Text("清空后导入") }
                                TextButton(
                                    onClick = { importVm.confirm(wipeFirst = false) },
                                    enabled = !importState.busy,
                                    modifier = Modifier.padding(start = 8.dp),
                                ) { Text("追加") }
                            }
                        } else {
                            Button(
                                onClick = { importVm.confirm(wipeFirst = false) },
                                enabled = !importState.busy,
                                modifier = Modifier.padding(top = 6.dp),
                            ) { Text("导入") }
                        }
                        TextButton(onClick = { importVm.dismiss() }) { Text("取消") }
                    }
                }
            }

            // ── 页脚：这一版是什么时候装的 ──────────────────────
            // 排查"你装的是哪一版"时，这一行比什么都省事
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text(installStamp(context), style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

/** `小助 v0.2.0(2) · 本版安装于 09-30 11:19` —— 括号里是会递增的 versionCode。 */
private fun installStamp(context: android.content.Context): String {
    val info = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        context.packageManager.getPackageInfo(
            context.packageName,
            android.content.pm.PackageManager.PackageInfoFlags.of(0),
        )
    } else {
        @Suppress("DEPRECATION")
        context.packageManager.getPackageInfo(context.packageName, 0)
    }
    val when_ = java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.getDefault())
        .format(java.util.Date(info.lastUpdateTime))
    val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) info.longVersionCode else {
        @Suppress("DEPRECATION") info.versionCode.toLong()
    }
    return "小助 v${info.versionName}($code) · 本版安装于 $when_"
}

/** 一行权限状态：绿＝已给，点了能跳去系统设置（多数权限只能在那儿改）。 */
@Composable
private fun PermissionRow(label: String, granted: Boolean, onOpen: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            if (granted) "✓" else "✗",
            style = MaterialTheme.typography.titleMedium,
            color = if (granted) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error,
        )
        Text(label, modifier = Modifier.weight(1f).padding(start = 8.dp))
        TextButton(onClick = onOpen, enabled = !granted) { Text("去设置") }
    }
}

/**
 * 图片占用那一行 + 「去清理」。
 *
 * 到上限**只提示不自动删**（用户明确要求）：图片是用户的东西，删不删由他决定。
 */
@Composable
private fun ImageStorageRow(onOpenImages: () -> Unit) {
    // LocalContext.current 不能在 remember 的 lambda 里取（那是非 @Composable 上下文）
    val ctx = LocalContext.current
    val store = remember { SettingsStore(ctx.applicationContext) }
    var stats by remember { mutableStateOf(com.xiaozhu.planner.core.image.ImageStore.stats()) }
    var cap by remember { mutableStateOf(store.imageCapBytes) }
    LaunchedEffect(Unit) {
        stats = withContext(Dispatchers.IO) { com.xiaozhu.planner.core.image.ImageStore.stats() }
    }
    val (count, bytes) = stats
    val over = cap > 0 && bytes > cap

    Text(
        "共 $count 张 · ${formatBytes(bytes)}" +
            if (cap > 0) " / 建议上限 ${formatBytes(cap)}" else "（不限制）",
        style = MaterialTheme.typography.bodyMedium,
        color = if (over) PlannerColors.Danger else MaterialTheme.colorScheme.onSurface,
        modifier = Modifier.padding(top = 4.dp),
    )
    Text(
        "图片存在手机里（不是数据库），发过的图会一直留着，方便以后问起时它能找回来。" +
            if (over) "已经超过建议上限了——去图片页筛出旧图，先导出再删。" else "",
        style = MaterialTheme.typography.bodySmall,
        modifier = Modifier.padding(top = 2.dp),
    )

    // 上限可选，但**只影响提示**，永远不自动删（用户明确要求）
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState())
            .padding(top = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        listOf(
            200L * 1024 * 1024 to "200 MB",
            500L * 1024 * 1024 to "500 MB",
            1024L * 1024 * 1024 to "1 GB",
            0L to "不限制",
        ).forEach { (value, label) ->
            FilterChip(
                selected = cap == value,
                onClick = {
                    store.imageCapBytes = value
                    cap = value
                },
                label = { Text(label) },
            )
        }
    }

    TextButton(onClick = onOpenImages, modifier = Modifier.padding(top = 2.dp)) {
        Text(if (over) "去清理图片" else "查看图片")
    }
}

private fun formatBytes(bytes: Long): String = when {
    bytes >= 1024L * 1024 * 1024 -> "%.1f GB".format(bytes / 1024.0 / 1024 / 1024)
    bytes >= 1024L * 1024 -> "%.0f MB".format(bytes / 1024.0 / 1024)
    else -> "%.0f KB".format(bytes / 1024.0)
}

/** 数字输入框（压缩参数用）：数字小键盘 + 标签 + 范围提示。 */
@Composable
private fun NumberField(
    label: String,
    hint: String,
    value: String,
    onValue: (String) -> Unit,
) {
    OutlinedTextField(
        value = value,
        // 只收数字（范围由 SettingsStore 夹紧，这里挡住明显非法的输入）
        onValueChange = { raw -> onValue(raw.filter { it.isDigit() }.take(3)) },
        label = { Text(label, style = MaterialTheme.typography.bodySmall) },
        supportingText = { Text(hint, style = MaterialTheme.typography.labelMedium) },
        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
            keyboardType = androidx.compose.ui.text.input.KeyboardType.Number,
        ),
        singleLine = true,
        modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
    )
}