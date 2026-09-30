package com.xiaozhu.planner.ui.chat

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.agent.AgentEvent
import com.xiaozhu.planner.core.agent.ConversationEngine
import com.xiaozhu.planner.core.agent.ProactivePrompts
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.llm.ChatMessage
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

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    init {
        refreshSettings()
        AppPresence.uiAlive = true
        viewModelScope.launch {
            ReminderEvents.fired.collect { lines -> onReminderFired(lines) }
        }
        viewModelScope.launch {
            HeartbeatEvents.fired.collect { note -> onHeartbeatFired(note) }
        }
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { engine.calendar.extendAllSeries() }
            runCatching { engine.scheduler.sync() }
            // 排心跳：没排过就兜底 30 分钟（PC 版同款），否则"主动找你"会永远不发生
            runCatching { engine.heartbeat.armWithFallback() }
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

    /** 输入框已经把恢复内容取走了，清掉这个一次性标记。 */
    fun consumeRestoreDraft() {
        _state.update { it.copy(restoreDraft = null) }
    }

    // ── 对话 ────────────────────────────────────────────────

    fun send(text: String) {
        val body = text.trim()
        if (body.isEmpty()) return
        speaker.stop()
        submit(ChatMessage.user(body), UiMessageKind.User, body)
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

    private fun submit(message: ChatMessage, kind: UiMessageKind, display: String) {
        if (_state.value.streaming) return
        if (!engine.settings.configured) {
            _state.update { it.copy(hint = "还没配置 API Key，先去设置页填一个。") }
            return
        }

        val userId = nextId++
        val assistantId = nextId++
        _state.update {
            it.copy(
                messages = it.messages +
                    UiMessage(id = userId, kind = kind, text = display, time = nowHHmm()) +
                    UiMessage(id = assistantId, kind = UiMessageKind.Assistant),
                streaming = true,
                hint = null,
                memoryNotice = null,
                restoreDraft = null,
            )
        }

        running = viewModelScope.launch {
            val restored = runCatching { engine.ensureLoaded() }.getOrDefault(false)
            if (restored) refreshMemoryStats()
            try {
                engine.runRound(message) { event -> onEvent(assistantId, event) }
            } catch (e: Exception) {
                patch(assistantId) { it.copy(text = it.text + "\n（中断：${e.message}）", failed = true) }
            } finally {
                _state.update { it.copy(streaming = false) }
            }
            describeCompression(engine.lastCompression)?.let { notice ->
                _state.update { it.copy(memoryNotice = "记忆更新：$notice") }
            }
            // 落盘后再回读一次：新消息要带上 rawIndex，否则它不可撤销
            reloadFromStore(keepTail = assistantId)
        }
    }

    // ── 历史 → 界面 ─────────────────────────────────────────

    /**
     * 从库里重建界面列表：**记忆卡片在前、原文在后**（与 buffer 的顺序一致）。
     *
     * @param keepTail 正在流式输出的那条临时消息（回读时不要把它抹掉）
     */
    suspend fun reloadFromStore(keepTail: Long? = null) {
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
                messages = if (tail != null) built + tail else built,
                memory = stats,
                historyLoaded = true,
                // token 估算与 PC 版一致：字符数 × 1.2（估算只会偏保守）
                contextTokens = estimateTokens(built),
            )
        }
        refreshLiveStatus()
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

    private fun onEvent(assistantId: Long, event: AgentEvent) {
        when (event) {
            is AgentEvent.TextDelta -> patch(assistantId) { it.copy(text = it.text + event.text) }

            is AgentEvent.ToolStarted -> {
                // continue_speaking 只是个停顿，PC 版两处都屏蔽它的卡片
                if (event.name in MessageTools.HIDDEN) return
                patch(assistantId) {
                    it.copy(tools = it.tools + UiTool(name = event.name, args = event.args, live = true))
                }
            }

            is AgentEvent.ToolFinished -> {
                if (event.name in MessageTools.HIDDEN) return
                patch(assistantId) { msg ->
                    val tools = msg.tools.toMutableList()
                    val idx = tools.indexOfLast { it.name == event.name && it.result == null }
                    if (idx >= 0) tools[idx] = tools[idx].copy(result = event.result, live = false)
                    msg.copy(tools = tools)
                }
            }

            is AgentEvent.Done -> patch(assistantId) {
                val final = it.text.ifBlank { "（这轮没有输出）" }
                speaker.speak(final)
                it.copy(text = final, time = nowHHmm())
            }

            is AgentEvent.Error -> patch(assistantId) {
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
