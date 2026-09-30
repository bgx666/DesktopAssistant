package com.xiaozhu.planner.ui.chat

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.agent.AgentEvent
import com.xiaozhu.planner.core.agent.ConversationEngine
import com.xiaozhu.planner.core.agent.ProactivePrompts
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.file.DocumentReader
import com.xiaozhu.planner.core.image.ImagePreparer
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.VisionSupport
import com.xiaozhu.planner.core.memory.CompressionOutcome
import com.xiaozhu.planner.core.memory.MemoryHit
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.NodeInfo
import com.xiaozhu.planner.core.memory.NodeRenderer
import com.xiaozhu.planner.core.migrate.SeedImport
import com.xiaozhu.planner.core.speech.Speaker
import com.xiaozhu.planner.data.db.MessageEntity
import com.xiaozhu.planner.data.db.NodeEntity
import com.xiaozhu.planner.proactive.AppPresence
import com.xiaozhu.planner.proactive.HeartbeatEvents
import com.xiaozhu.planner.reminder.ReminderEvents
import com.xiaozhu.planner.speech.AppSpeaker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * 聊天页的中枢：把 UI 事件翻译成引擎调用，把引擎事件画到界面上。
 *
 * 分工：
 * - **"想一轮"在 [ConversationEngine]**（单例）——心跳/提醒在后台也要用它
 * - 这里只管：**把已恢复的历史画出来**、上屏、朗读、撤销、记忆概览
 * - **Room 是 buffer 的唯一事实源**；界面上的列表是它的一个视图
 *
 * 关于历史：早先这里只在发送时往后追加，于是"导入了 203 个记忆节点 + 64 条对话"
 * 在界面上完全看不见——用户以为记忆没搬过来。现在启动就读库渲染。
 */
class ChatViewModel(app: Application) : AndroidViewModel(app) {

    private val engine = ConversationEngine.get(app)

    /** 朗读：MiMo 优先、系统 TTS 兜底（见 AppSpeaker）。 */
    private val speaker: Speaker = AppSpeaker(app, engine.settings)

    private var running: Job? = null
    private var nextId = 1L

    /**
     * 正在接收流式文字的那个气泡。
     *
     * 一轮里模型可能被调用多次（调工具前后各一次），**每次输出是独立的一条消息**
     * ——库里就是这么存的，PC 版也是这么显示的。所以工具一出现就把当前气泡"封口"，
     * 后面的文字另起一个气泡；否则界面上的"拼接版"与回读后的"分开版"对不上，
     * 用户会看到同一段话出现两次。
     */
    private var currentAssistantId = 0L
    private var segmentClosed = false

    /** 这一轮开始前库里的原文条数（判断落盘成没成，不看文本）。 */
    private var rawCountBeforeRound: Int? = null


    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    init {
        // 端点迁移要在读设置之前做：把"沿用的旧默认值"换成新默认（只动从没手改过的）
        val migrated = runCatching { engine.settings.migrateLegacyEndpointIfNeeded() }.getOrDefault(false)
        refreshSettings()
        if (migrated) {
            _state.update {
                it.copy(
                    memoryNotice = "已把模型端点切到 DeepSeek 官方（${engine.settings.baseUrl}，" +
                        "模型 ${engine.settings.model}）——实测比原来的网关快约三倍"
                )
            }
        }
        AppPresence.uiAlive = true
        viewModelScope.launch {
            ReminderEvents.fired.collect { lines -> onReminderFired(lines) }
        }
        viewModelScope.launch {
            HeartbeatEvents.fired.collect { note -> onHeartbeatFired(note) }
        }
        // 压缩是后台跑的（不阻塞对话），完成后通知界面：提示 + 重画记忆卡片
        viewModelScope.launch {
            engine.compressionEvents.collect { outcomes ->
                describeCompression(outcomes)?.let { notice ->
                    _state.update { it.copy(memoryNotice = "记忆更新：$notice") }
                }
                reloadFromStore()
            }
        }
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { engine.calendar.extendAllSeries() }
            runCatching { engine.scheduler.sync() }
            // 排心跳：没排过就兜底 30 分钟（PC 版同款），否则"主动找你"会永远不发生
            runCatching { engine.heartbeat.armWithFallback() }
            // 图片迁移：把库里老格式的内嵌 base64 搬进文件。每次一小批、逐行独立，
            // 失败的行原样留着 base64 下次继续——见 ImageMigration（迁移不可能丢图）
            runCatching { com.xiaozhu.planner.core.image.ImageMigration.runBatch(engine.db) }
        }
        // 把库里的历史读出来画上（这是"记忆搬过来了"唯一看得见的地方）
        viewModelScope.launch {
            // 随包带进来的旧数据：首次启动自动导入（装完即迁移完，不用传文件/选文件）
            val seeded = withContext(Dispatchers.IO) {
                runCatching { SeedImport.importIfNeeded(getApplication(), engine.db) }.getOrNull()
            }
            if (seeded is SeedImport.Outcome.Imported) {
                _state.update {
                    it.copy(
                        memoryNotice = "已导入电脑上的记忆：节点 ${seeded.nodes} 个、" +
                            "对话 ${seeded.messages} 条、日程 ${seeded.calendar} 项"
                    )
                }
            } else {
                // 库非空 → 自动路径跳过了。**不能一声不响**：问一句
                refreshSeedPrompt()
            }
            reloadFromStore()
        }
        // 标题栏的心跳倒计时与逾期角标要跟着时间走（PC 版靠后端推 state，这边自己滴答）
        viewModelScope.launch {
            while (true) {
                refreshLiveStatus()
                kotlinx.coroutines.delay(30_000)
            }
        }
    }

    override fun onCleared() {
        AppPresence.uiAlive = false
        super.onCleared()
    }

    fun refreshSettings() {
        _state.update { it.copy(configured = engine.settings.configured, hint = null) }
    }

    fun showHint(text: String) {
        _state.update { it.copy(hint = text) }
    }

    // ── 随包旧数据（装完自动导入；库非空时问一句，不静默跳过）──────

    private suspend fun refreshSeedPrompt() {
        val app = getApplication<Application>()
        val pending = withContext(Dispatchers.IO) {
            runCatching { SeedImport.isPending(app, engine.db) }.getOrDefault(false)
        }
        if (!pending) {
            _state.update { it.copy(seedPrompt = null) }
            return
        }
        val counts = withContext(Dispatchers.IO) { SeedImport.peekCounts(app) }
        val stats = withContext(Dispatchers.IO) { memoryStatsInternal() }
        _state.update {
            it.copy(
                seedPrompt = SeedPrompt(
                    nodes = counts?.first ?: 0,
                    calendar = counts?.second ?: 0,
                    currentNodes = stats.totalNodes,
                    currentMessages = stats.rawMessages,
                )
            )
        }
    }

    /** 用户点「导入并替换」：清掉手机上的，换成包里的旧数据。 */
    fun importSeedNow() {
        viewModelScope.launch {
            _state.update { it.copy(seedPrompt = null, memoryNotice = "正在导入…") }
            val outcome = withContext(Dispatchers.IO) {
                runCatching {
                    SeedImport.importIfNeeded(getApplication(), engine.db, force = true)
                }.getOrNull()
            }
            val text = when (outcome) {
                is SeedImport.Outcome.Imported ->
                    "已导入电脑上的记忆：节点 ${outcome.nodes} 个、" +
                        "对话 ${outcome.messages} 条、日程 ${outcome.calendar} 项"
                SeedImport.Outcome.Skipped -> "包里没有可导入的旧数据。"
                is SeedImport.Outcome.Failed -> "导入失败：${outcome.reason}（手机上原有的没动）"
                null -> "导入失败（读不出包里的数据）。"
            }
            // 换了库 → 引擎里那份内存历史必须作废重建，否则下一轮还带着旧上下文
            runCatching { engine.forgetHistory() }
            _state.update { it.copy(memoryNotice = text) }
            reloadFromStore()
        }
    }

    /** 用户点「先不用」：记标记，别每次都问。 */
    fun dismissSeedPrompt() {
        SeedImport.dismiss(getApplication())
        _state.update { it.copy(seedPrompt = null) }
    }

    // ── 图片输入（拍照 / 相册）─────────────────────────────

    /** 把一张 data URL 放进待发区（含数量上限检查）。 */
    private fun addImage(dataUrl: String) {
        _state.update {
            if (it.pendingImages.size >= ImagePreparer.MAX_IMAGES) {
                it.copy(hint = "一次最多带 ${ImagePreparer.MAX_IMAGES} 张图。")
            } else {
                it.copy(pendingImages = it.pendingImages + dataUrl, hint = null)
            }
        }
    }

    /**
     * 把一张图放进"待发送"区（已经缩小摆正过）。
     *
     * 模型看不了图时**直接拦下并说清楚**——发出去只会得到一句看不懂的 400。
     * PC 版在这种情况下会退回 OCR，安卓没有 OCR（不想为它背一个 ML Kit 依赖），
     * 所以选择明确告知 + 建议换模型。
     */
    fun attachImage(uri: android.net.Uri) {
        val model = engine.settings.model
        if (!VisionSupport.isVisionCapable(model)) {
            showHint(VisionSupport.unsupportedHint(model))
            return
        }
        viewModelScope.launch {
            val dataUrl = withContext(Dispatchers.IO) {
                runCatching { ImagePreparer.toDataUrl(getApplication(), uri) }.getOrNull()
            }
            if (dataUrl == null) {
                // 说清是哪种读不出来：空文件（相机没写成）、不是图片、还是打不开
                val reason = withContext(Dispatchers.IO) {
                    runCatching { ImagePreparer.diagnose(getApplication(), uri) }.getOrNull()
                } ?: "这张图读不出来。"
                showHint(reason)
                return@launch
            }
            addImage(dataUrl)
        }
    }

    fun removePendingImage(index: Int) {
        _state.update {
            it.copy(pendingImages = it.pendingImages.filterIndexed { i, _ -> i != index })
        }
    }

    /** 移除已附加但还没发送的文档（用户报过：之前加了就没法删）。 */
    fun removePendingDocument() {
        _state.update { it.copy(pendingText = null, pendingDocName = null) }
    }

    /**
     * 选一个本地文档当附件。
     *
     * 三种归宿（见 [DocumentReader]）：纯文本/docx 读成正文拼进消息、
     * PDF 渲染成图交给视觉模型、读不了的只报名字并说明。
     * 正文先放在"待发"里（用户可以看一眼再发），发送时并进消息文本。
     */
    fun attachDocument(uri: android.net.Uri, displayName: String?) {
        viewModelScope.launch {
            val result = withContext(Dispatchers.IO) {
                runCatching { DocumentReader.read(getApplication(), uri, displayName) }.getOrNull()
            }
            when (result) {
                null -> showHint("读这个文件失败。")
                is DocumentReader.Result.Unsupported -> showHint(result.reason)
                is DocumentReader.Result.Text -> _state.update {
                    it.copy(
                        pendingText = DocumentReader.renderText(result),
                        hint = null,
                    )
                }
                is DocumentReader.Result.Images -> {
                    val model = engine.settings.model
                    if (!VisionSupport.isVisionCapable(model)) {
                        // PDF 只能走"看图"这条路，模型看不了图就没法读
                        showHint("PDF 是渲染成图片读的，而${VisionSupport.unsupportedHint(model)}")
                        return@launch
                    }
                    _state.update {
                        val room = ImagePreparer.MAX_IMAGES - it.pendingImages.size
                        if (room <= 0) {
                            it.copy(hint = "一次最多带 ${ImagePreparer.MAX_IMAGES} 张图。")
                        } else {
                            it.copy(
                                pendingImages = it.pendingImages + result.dataUrls.take(room),
                                pendingDocName = result.name,
                                hint = null,
                            )
                        }
                    }
                }
            }
        }
    }

    /** 把待发的附件正文并进消息（发送时调用）。 */
    private fun mergePendingText(body: String): String {
        val attachment = _state.value.pendingText ?: return body
        return if (body.isBlank()) attachment else "$body\n\n$attachment"
    }

    /** 输入框已经把恢复内容取走了，清掉这个一次性标记。 */
    fun consumeRestoreDraft() {
        _state.update { it.copy(restoreDraft = null) }
    }

    // ── 对话 ────────────────────────────────────────────────

    fun send(text: String) {
        val body = mergePendingText(text.trim())
        val images = _state.value.pendingImages
        // 只有图/只有附件、没有字也可以发（"看看这张""读一下这个"这种）
        if (body.isEmpty() && images.isEmpty()) return
        speaker.stop()
        val message = if (images.isEmpty()) {
            ChatMessage.user(body)
        } else {
            ChatMessage.userWithImages(body, images)
        }
        // 界面气泡里不重复贴整篇附件正文（那会把界面刷屏），只显示用户自己打的字
        submit(message, UiMessageKind.User, text.trim().ifBlank { "（附件）" }, images)
    }

    fun speak(text: String) = speaker.speak(text)

    fun stopSpeaking() = speaker.stop()

    fun stop() {
        running?.cancel()
        running = null
        _state.update { it.copy(streaming = false) }
    }

    /**
     * 撤销：撤回这条**及其之后**的对话，内容放回输入框（与 PC 版 `/undo` 同义）。
     *
     * 只对原文消息有效；记忆卡片（已压缩进记忆树的历史）不能被撤销——
     * 那些内容已经变成长期记忆了，删掉它等于删记忆，不是"撤回一句话"。
     */
    fun undo(messageId: Long) {
        val target = _state.value.messages.firstOrNull { it.id == messageId } ?: return
        val rawIndex = target.rawIndex
        if (rawIndex == null) {
            showHint("这条已经在长期记忆里了，撤不回；要改就用一句话跟它说清楚。")
            return
        }
        viewModelScope.launch {
            val removed = withContext(Dispatchers.IO) {
                runCatching { engine.truncateFromRaw(rawIndex) }.getOrDefault(false)
            }
            if (!removed) {
                showHint("撤销失败（没能改写历史），原文没动。")
                return@launch
            }
            _state.update { it.copy(restoreDraft = target.text) }
            reloadFromStore()
        }
    }

    private fun onReminderFired(lines: List<String>) {
        val text = ProactivePrompts.reminder(CalendarDates.now().format(HHMM), lines)
        submit(ChatMessage.user(text), UiMessageKind.System, lines.joinToString("\n"))
    }

    private fun onHeartbeatFired(note: String) {
        val text = ProactivePrompts.heartbeat(CalendarDates.now().format(HHMM), note)
        submit(
            ChatMessage.user(text),
            UiMessageKind.System,
            note.ifBlank { "（我醒了一下）" },
        )
    }

    private fun submit(
        message: ChatMessage,
        kind: UiMessageKind,
        display: String,
        images: List<String> = emptyList(),
    ) {
        if (_state.value.streaming) return
        if (!engine.settings.configured) {
            _state.update { it.copy(hint = "还没配置 API Key，先去设置页填一个。") }
            return
        }

        val userId = nextId++
        val assistantId = nextId++
        currentAssistantId = assistantId
        segmentClosed = false
        _state.update {
            // 给新消息算上原文序号：否则"刚发完那几秒"撤销按钮是消失的
            val newRawIndex = rawIndexOfNewMessage(it.messages, it.historyLoaded)
            it.copy(
                messages = it.messages +
                    UiMessage(
                        id = userId,
                        kind = kind,
                        text = display,
                        time = nowHHmm(),
                        rawIndex = newRawIndex,
                        images = images,
                    ) +
                    UiMessage(id = assistantId, kind = UiMessageKind.Assistant),
                streaming = true,
                hint = null,
                memoryNotice = null,
                restoreDraft = null,
                // 图与附件正文已经随这条消息发出去了，待发区清空（同 PC 的 clearMounted）
                pendingImages = emptyList(),
                pendingText = null,
                pendingDocName = null,
            )
        }

        running = viewModelScope.launch {
            rawCountBeforeRound = withContext(Dispatchers.IO) {
                runCatching { engine.store.rawCount() }.getOrNull()
            }
            val restored = runCatching { engine.ensureLoaded() }.getOrDefault(false)
            if (restored) refreshMemoryStats()
            try {
                engine.runRound(message) { event -> onEvent(event) }
            } catch (e: Exception) {
                patch(currentAssistantId) { it.copy(text = it.text + "\n（中断：${e.message}）", failed = true) }
            } finally {
                _state.update { it.copy(streaming = false) }
            }
            // 落盘后再回读一次：新消息要带上 rawIndex（撤销要用），
            // 并按"有没有新原文落库"决定内存里那份气泡的去留
            reloadFromStore(keepTail = currentAssistantId, rawBefore = rawCountBeforeRound)
        }
    }

    // ── 历史 → 界面 ─────────────────────────────────────────

    /**
     * 从库里重建界面列表：**记忆卡片在前、原文在后**（与 buffer 的顺序一致）。
     *
     * @param keepTail 正在流式输出的那条临时消息（回读时按"有没有落盘"决定去留）
     * @param rawBefore 这一轮开始前库里有多少条原文（null = 不判断，直接保留 tail）
     * @return 回读后库里的原文条数
     */
    suspend fun reloadFromStore(keepTail: Long? = null, rawBefore: Int? = null): Int {
        val snapshot = withContext(Dispatchers.IO) {
            val nodes = engine.store.nodeMessages()
            val raw = engine.store.rawMessages()
            val stats = memoryStatsInternal()
            Triple(nodes, raw, stats)
        }
        val (nodes, raw, stats) = snapshot

        val built = ArrayList<UiMessage>(nodes.size + raw.size)
        nodes.forEach { row ->
            built += memoryCard(row)
        }

        // PC 版 `/history` 的渲染规则（server.py:218-243），逐条照做：
        // 1. `（`/`[当前` 开头的 human 是**系统注入**（提醒、心跳、议程、日历操作注记），
        //    **不显示**——它们是"小助在跟你说话"，不是用户说的。当成用户气泡显示，
        //    用户会看到一堆自己从没说过的话（导入历史里就有）。
        // 2. `[` 开头且含 `对你说：` → 只取后半段（去掉时间前缀）。
        // 3. tool 消息不单独成行：结果填回对应的工具卡片。
        // 4. `continue_speaking` 的卡片两处都屏蔽（它只是个停顿，不是工具调用）。
        val resultsByCallId = raw
            .filter { it.role == "tool" && !it.toolCallId.isNullOrBlank() }
            .associate { it.toolCallId!! to it.content }

        raw.forEachIndexed { index, row ->
            when (row.role) {
                "tool" -> Unit // 结果已经填进卡片
                "user" -> {
                    val text = userVisibleText(row.content) ?: return@forEachIndexed
                    built += rawBubble(row.copy(content = text), index)
                }
                else -> built += rawBubble(row, index, resultsByCallId)
            }
        }

        _state.update { current ->
            val tail = keepTail?.let { id ->
                current.messages.lastOrNull { it.id == id && it.kind == UiMessageKind.Assistant }
            }
            current.copy(
                // 判据是"这一轮有没有新原文落进库"，不是文本比对（见 HistoryMerge 的注释）
                messages = HistoryMerge.merge(built, tail, rawBefore, raw.size),
                memory = stats,
                historyLoaded = true,
                // token 估算与 PC 版一致：字符数 × 1.2（估算只会偏保守）
                contextTokens = estimateTokens(built),
            )
        }
        refreshLiveStatus()
        return raw.size
    }

    /** PC 版 `context_stats()`：`tokens = int(chars * 1.2)`。 */
    private fun estimateTokens(messages: List<UiMessage>): Int {
        val chars = messages.sumOf { it.text.length + it.tools.sumOf { t -> t.args.length } }
        return (chars * 1.2).toInt()
    }

    /**
     * 标题栏那两个跟时间有关的显示：心跳倒计时 + 逾期角标。
     *
     * PC 版是后端每次推 `state` 时带上；安卓这边没有那个推送，就自己定期刷。
     */
    private suspend fun refreshLiveStatus() {
        val (label, overdue) = withContext(Dispatchers.IO) {
            val left = engine.heartbeat.remainingMillis()
            val note = engine.heartbeatNote()
            // PC 文案：<60 秒说"秒后醒来"，否则"分钟后醒来"；有备注就缀在括号里
            val text = left?.let {
                val seconds = it / 1000
                val base = if (seconds < 60) "${seconds} 秒后醒来" else "${seconds / 60} 分钟后醒来"
                if (note.isBlank()) base else "$base：$note"
            }
            val overdue = runCatching { engine.calendar.overdue().size }.getOrDefault(0)
            text to overdue
        }
        _state.update { it.copy(heartbeatLabel = label, overdueCount = overdue) }
    }

    /**
     * 用户在界面上该看到的正文（PC 版 `server.py:222-228` 的规则）。
     *
     * 返回 null = **这条不该显示**：`（`/`[当前` 开头的是系统注入
     * （提醒、心跳、议程快照、日历操作注记），模型看得到但用户不该看到。
     */
    private fun userVisibleText(content: String): String? {
        val text = content.trim()
        if (text.isEmpty()) return null
        if (text.startsWith("（") || text.startsWith("[当前")) return null
        if (text.startsWith("[")) {
            val marker = "对你说："
            val at = text.indexOf(marker)
            return if (at >= 0) text.substring(at + marker.length).trim().ifEmpty { null } else null
        }
        return text
    }

    private fun memoryCard(row: MessageEntity): UiMessage {
        val nodeId = row.nodeId ?: "node?"
        val level = nodeId.removePrefix("node").substringBefore('_').toIntOrNull() ?: 0
        // PC 版标题是 CSS 的 ::before 固定文案
        val title = if (level == 0) "记忆摘要（早期对话已压缩）" else "记忆摘要（更早的对话已上卷）"
        return UiMessage(
            id = nextId++,
            kind = UiMessageKind.Memory,
            text = row.content,
            memoryMeta = "$title · $nodeId",
        )
    }

    private fun rawBubble(
        row: MessageEntity,
        index: Int,
        resultsByCallId: Map<String, String> = emptyMap(),
    ): UiMessage {
        val kind = when (row.role) {
            "user" -> UiMessageKind.User
            else -> UiMessageKind.Assistant
        }
        return UiMessage(
            id = nextId++,
            kind = kind,
            text = row.content,
            time = hhmm(row.tsMillis),
            rawIndex = index,
            // 历史里的工具卡片是**已完成**态（不该转圈），结果按 tool_call_id 填回去
            tools = MessageTools.parse(row.toolCalls, row.name, resultsByCallId, live = false),
            // 图片从库里读回来（常驻 buffer，重开 app 也还在）
            images = com.xiaozhu.planner.core.memory.MessageMapping.imagesOf(row),
        )
    }

    // ── 记忆 ────────────────────────────────────────────────

    private suspend fun memoryStatsInternal(): MemoryStats {
        val store = engine.store
        return MemoryStats(
            totalNodes = store.nodeCount(),
            levels = (0..4).map { it to store.levelCount(it) }.filter { it.second > 0 },
            rootId = store.rootId(),
            rawMessages = store.rawCount(),
        )
    }

    suspend fun memoryStats(): MemoryStats = withContext(Dispatchers.IO) { memoryStatsInternal() }

    private suspend fun refreshMemoryStats() {
        val stats = withContext(Dispatchers.IO) { memoryStatsInternal() }
        _state.update { it.copy(memory = stats) }
    }

    suspend fun memoryNodesAt(level: Int): List<NodeEntity> = withContext(Dispatchers.IO) {
        engine.store.activeAtLevel(level)
    }

    suspend fun memoryNodeInfo(nodeId: String): NodeInfo? = withContext(Dispatchers.IO) {
        engine.store.nodeInfo(nodeId)
    }

    suspend fun memorySearch(query: String): List<MemoryHit> = withContext(Dispatchers.IO) {
        engine.store.search(query, limit = 10)
    }

    /** 给记忆页用的"展开成一段文字"（与 PC 版的展开格式一致）。 */
    fun renderNode(info: NodeInfo): String = NodeRenderer.renderForTool(info)

    // ── 事件 → UI ───────────────────────────────────────────

    private fun onEvent(event: AgentEvent) {
        when (event) {
            is AgentEvent.TextDelta -> {
                // 上一次工具调用之后又出文字 = 模型被调用了新的一次 → 另起一个气泡
                // （库里就是分开存的两条消息，界面对齐它才不会出现"同一段话两遍"）
                if (segmentClosed) {
                    val fresh = nextId++
                    currentAssistantId = fresh
                    segmentClosed = false
                    _state.update {
                        it.copy(messages = it.messages + UiMessage(id = fresh, kind = UiMessageKind.Assistant))
                    }
                }
                patch(currentAssistantId) { it.copy(text = it.text + event.text) }
            }

            is AgentEvent.ToolStarted -> {
                // continue_speaking 只是个停顿，PC 版两处都屏蔽它的卡片
                if (event.name in MessageTools.HIDDEN) return
                patch(currentAssistantId) {
                    it.copy(tools = it.tools + UiTool(name = event.name, args = event.args, live = true))
                }
                // 封口：后面再出文字就是新的一次模型调用
                segmentClosed = true
            }

            is AgentEvent.ToolFinished -> {
                if (event.name in MessageTools.HIDDEN) return
                // 工具属于"发起它的那个气泡"，所以要往前找（可能已经不是当前气泡了）
                _state.update { s ->
                    val idx = s.messages.indexOfLast { msg ->
                        msg.tools.any { it.name == event.name && it.result == null }
                    }
                    if (idx < 0) s
                    else s.copy(
                        messages = s.messages.mapIndexed { i, msg ->
                            if (i != idx) msg
                            else msg.copy(
                                tools = msg.tools.map { t ->
                                    if (t.name == event.name && t.result == null) {
                                        t.copy(result = event.result, live = false)
                                    } else t
                                }
                            )
                        }
                    )
                }
            }

            is AgentEvent.Done -> patch(currentAssistantId) {
                // 只调了工具、没有说话：留空（界面据此不画气泡，只留工具卡片）。
                // 一个字都没有、也没有工具时才给个占位，免得用户觉得它没理人。
                val final = when {
                    it.text.isNotBlank() -> it.text
                    it.tools.isNotEmpty() -> ""
                    else -> "（这轮没有输出）"
                }
                if (final.isNotBlank()) speaker.speak(final)
                it.copy(text = final, time = nowHHmm())
            }

            is AgentEvent.Error -> patch(currentAssistantId) {
                it.copy(text = (it.text + "\n出错了：" + event.message).trim(), failed = true)
            }
        }
    }

    private fun patch(id: Long, transform: (UiMessage) -> UiMessage) {
        _state.update { s -> s.copy(messages = s.messages.map { if (it.id == id) transform(it) else it }) }
    }

    private companion object {
        val HHMM: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")

        fun nowHHmm(): String = CalendarDates.now().format(HHMM)

        fun hhmm(millis: Long): String? =
            if (millis <= 0) null
            else Instant.ofEpochMilli(millis).atZone(ZoneId.of("Asia/Shanghai")).format(HHMM)
    }
}

/** 记忆更新的提示文案（引擎给的是结构化结果，人话在这里拼）。 */
internal fun describeCompression(outcomes: List<CompressionOutcome>): String? {
    val notable = outcomes.filter { it !is CompressionOutcome.Skipped }
    if (notable.isEmpty()) return null
    return notable.joinToString("；") { outcome ->
        when (outcome) {
            is CompressionOutcome.LeafCreated -> "把 ${outcome.messageCount} 条对话压成记忆节点 ${outcome.nodeId}"
            is CompressionOutcome.ParentCreated -> "把 ${outcome.childCount} 个节点上卷成 ${outcome.nodeId}"
            is CompressionOutcome.Failed -> "压缩失败（${outcome.reason}），原文保留"
            CompressionOutcome.Skipped -> ""
        }
    }
}

/**
 * 历史里的工具调用：原文只存了 `tool_calls` 的 JSON，这里还原成卡片要的信息。
 *
 * 与 PC 版一致的两点：
 * - **历史卡片是完成态**（不转圈）——`live = false`
 * - `continue_speaking` **两处都屏蔽**（实时与历史）：它只是个停顿，不是真的调了工具
 */
internal object MessageTools {

    /** 不展示的工具（PC 版 server.py:237-243 与 session.py:1407-1417 都跳过它）。 */
    val HIDDEN = setOf("continue_speaking")

    fun parse(
        toolCallsJson: String?,
        name: String?,
        resultsByCallId: Map<String, String> = emptyMap(),
        live: Boolean = false,
    ): List<UiTool> {
        if (toolCallsJson.isNullOrBlank()) return emptyList()
        return runCatching {
            com.xiaozhu.planner.core.llm.PlannerJson
                .decodeFromString(
                    kotlinx.serialization.builtins.ListSerializer(
                        com.xiaozhu.planner.core.llm.ToolCall.serializer()
                    ),
                    toolCallsJson,
                )
                .filter { it.function.name !in HIDDEN }
                .map {
                    UiTool(
                        name = it.function.name,
                        args = it.function.arguments,
                        result = resultsByCallId[it.id],
                        live = live,
                    )
                }
        }.getOrDefault(emptyList())
    }
}
