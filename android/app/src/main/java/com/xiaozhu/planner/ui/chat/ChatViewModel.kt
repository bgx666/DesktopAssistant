package com.xiaozhu.planner.ui.chat

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.agent.AgentEvent
import com.xiaozhu.planner.core.agent.AgentLoop
import com.xiaozhu.planner.core.agent.ConversationService
import com.xiaozhu.planner.core.agent.SpeechPacing
import com.xiaozhu.planner.core.agent.SystemPrompt
import com.xiaozhu.planner.core.agent.ToolRegistry
import com.xiaozhu.planner.core.calendar.AgendaSnapshot
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.OpenAiClient
import com.xiaozhu.planner.core.memory.CompressionOutcome
import com.xiaozhu.planner.core.memory.Compressor
import com.xiaozhu.planner.core.memory.MemoryHit
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.NodeInfo
import com.xiaozhu.planner.core.tools.AddScheduleTool
import com.xiaozhu.planner.core.tools.CancelScheduleTool
import com.xiaozhu.planner.core.tools.ContinueSpeakingTool
import com.xiaozhu.planner.core.tools.ExploreMemoryTreeTool
import com.xiaozhu.planner.core.tools.HeartbeatTool
import com.xiaozhu.planner.core.tools.ListScheduleTool
import com.xiaozhu.planner.core.tools.SearchMemoryTool
import com.xiaozhu.planner.core.tools.SetDoNotDisturbTool
import com.xiaozhu.planner.core.tools.UpdateScheduleTool
import com.xiaozhu.planner.core.tools.WebFetchTool
import com.xiaozhu.planner.core.tools.WebSearchTool
import com.xiaozhu.planner.data.PrefsDndStore
import com.xiaozhu.planner.data.PrefsHeartbeatStore
import com.xiaozhu.planner.data.SettingsStore
import com.xiaozhu.planner.data.db.NodeEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import com.xiaozhu.planner.reminder.ReminderEvents
import com.xiaozhu.planner.reminder.ReminderScheduler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * 会话中枢：设置 → 客户端 → agent 循环 → UI 状态，记忆落 Room，日程走日历库。
 *
 * 分工：
 * - **Room 是 buffer 的唯一事实源**；内存里的 [history] 只是它的视图（压缩后重建）
 * - 用户消息一进来就上屏（体感快），落盘与压缩在协程里做
 * - 压缩失败**绝不影响这一轮对话**（对话优先，记忆是渐进增强）
 * - 日程变更后统一 [ReminderScheduler.sync] 重排闹钟
 */
class ChatViewModel(app: Application) : AndroidViewModel(app) {

    private val settings = SettingsStore(app)
    private val db = PlannerDatabase.get(app)
    private val store = MemoryStore(db)
    private val calendar = CalendarStore(db.calendar())
    private val conversation = ConversationService(store)
    private val scheduler = ReminderScheduler(app, calendar, PrefsDndStore(app))

    /** `continue_speaking` 的说话节奏（跨轮沿用，跟 PC 版的 session 状态对齐）。 */
    private val pacing = SpeechPacing()

    private val registry = ToolRegistry(
        listOf(
            WebSearchTool(),
            WebFetchTool(),
            HeartbeatTool(PrefsHeartbeatStore(app)),
            ContinueSpeakingTool(pacing),
            ExploreMemoryTreeTool(store),
            SearchMemoryTool(store),
            ListScheduleTool(calendar),
            AddScheduleTool(calendar, scheduler),
            UpdateScheduleTool(calendar, scheduler),
            CancelScheduleTool(calendar, scheduler),
            SetDoNotDisturbTool(PrefsDndStore(app)),
        )
    )

    private val history = mutableListOf<ChatMessage>()
    private var loaded = false
    private var running: Job? = null
    private var nextId = 1L

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    init {
        // 冷启动就要知道"配没配 key"，否则用户第一次发消息才被告知没配置
        refreshSettings()
        // 提醒到点 → 让模型说一句（App 正开着才收得到；没开时用户看到的是通知栏本身）
        viewModelScope.launch {
            ReminderEvents.fired.collect { lines -> onReminderFired(lines) }
        }
        // 启动时收拾一次：续期重复系列 + 重排闹钟（重启后 AlarmManager 是空的）
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { calendar.extendAllSeries() }
            runCatching { scheduler.sync() }
        }
    }

    fun refreshSettings() {
        _state.update { it.copy(configured = settings.configured, hint = null) }
    }

    // ── 对话 ────────────────────────────────────────────────

    fun send(text: String) {
        val body = text.trim()
        if (body.isEmpty()) return
        submit(ChatMessage.user(body), fromUser = true, display = body)
    }

    fun stop() {
        running?.cancel()
        running = null
        _state.update { it.copy(streaming = false) }
    }

    /**
     * 提醒到点时由系统触发的一轮（不是用户说的话）。
     *
     * 落盘的是一条 `（系统：…）` 开头的 user 消息——与 PC 版一致：模型被告知
     * "这是时间提示，不是用户在说话"，但仍按对话历史记住它。
     */
    private fun onReminderFired(lines: List<String>) {
        val now = CalendarDates.now()
        val text = "（系统：现在 ${now.format(HHMM)}，" +
            "日历上到点的是：${lines.joinToString("；")}。" +
            "这是用户自己排的安排——你判断该提醒他、还是该先自己处理、" +
            "或者其实已经做过了（那就顺势问一句）。）"
        submit(ChatMessage.user(text), fromUser = false, display = lines.joinToString("\n"), fromSystem = true)
    }

    private fun submit(
        message: ChatMessage,
        fromUser: Boolean,
        display: String,
        fromSystem: Boolean = false,
    ) {
        if (_state.value.streaming) return
        if (!settings.configured) {
            _state.update { it.copy(hint = "还没配置 API Key，先去设置页填一个。") }
            return
        }

        val userId = nextId++
        val assistantId = nextId++
        _state.update {
            it.copy(
                messages = it.messages +
                    UiMessage(id = userId, fromUser = fromUser, fromSystem = fromSystem, text = display) +
                    UiMessage(id = assistantId, fromUser = false),
                streaming = true,
                hint = null,
                memoryNotice = null,
            )
        }

        running = viewModelScope.launch {
            ensureLoaded()
            ensureSystemPrompt()
            // 先落盘用户/系统消息：中途被杀也不至于把用户刚说的话弄丢。
            // before 必须在这之后再取，否则这条会被 persist 两次（恢复时看到两份）。
            history += message
            val before = history.size
            runCatching { conversation.persist(listOf(message), System.currentTimeMillis()) }

            val loop = AgentLoop(
                OpenAiClient(settings.baseUrl, settings.apiKey, settings.model),
                registry,
                pacing = pacing,
            )
            try {
                loop.run(history).collect { event -> onEvent(assistantId, event) }
            } catch (e: Exception) {
                patch(assistantId) { it.copy(text = it.text + "\n（中断：${e.message}）") }
            } finally {
                _state.update { it.copy(streaming = false) }
            }

            // 本轮新增消息落盘（含工具调用配对）
            runCatching { conversation.persist(history.drop(before), System.currentTimeMillis()) }
            runCatching { saveState() }
            runCatching { compressIfNeeded() }
            // 模型可能刚改过日程（工具内部也会 sync，这里兜一层：重复调用是幂等的）
            runCatching { scheduler.sync() }
        }
    }

    // ── 记忆 ────────────────────────────────────────────────

    suspend fun memoryStats(): MemoryStats = withContext(Dispatchers.IO) {
        val levels = (0..4).map { it to store.levelCount(it) }.filter { it.second > 0 }
        MemoryStats(
            totalNodes = store.nodeCount(),
            levels = levels,
            rootId = store.rootId(),
            rawMessages = store.rawCount(),
        )
    }

    suspend fun memoryNodesAt(level: Int): List<NodeEntity> = withContext(Dispatchers.IO) {
        store.activeAtLevel(level)
    }

    suspend fun memoryNodeInfo(nodeId: String): NodeInfo? = withContext(Dispatchers.IO) {
        store.nodeInfo(nodeId)
    }

    suspend fun memorySearch(query: String): List<MemoryHit> = withContext(Dispatchers.IO) {
        store.search(query, limit = 10)
    }

    // ── 内部 ────────────────────────────────────────────────

    private suspend fun ensureLoaded() {
        if (loaded) return
        val state = conversation.loadState()
        history += conversation.loadHistory()
        if (state.round > 0 || state.msgCounter > 0) {
            _state.update {
                it.copy(memoryNotice = "已恢复上次的对话与记忆（原文 ${store.rawCount()} 条）")
            }
        }
        loaded = true
    }

    /**
     * system prompt 每轮重建：日期、议程、可用工具清单都要是最新的。
     *
     * 议程走「今天 + 未来 7 天」，并且**折叠重复项**（否则"每天吃药"会把窗口占满）。
     */
    private suspend fun systemPromptText(): String {
        val today = CalendarDates.todayText()
        val agenda = runCatching {
            AgendaSnapshot.render(
                today = today,
                items = calendar.range(today, CalendarDates.plusDays(today, AgendaSnapshot.WINDOW_DAYS) ?: today),
            )
        }.getOrNull()
        return SystemPrompt.build(
            template = getApplication<Application>().assets
                .open("prompts/system.md").bufferedReader().use { it.readText() },
            now = ZonedDateTime.now(ZoneId.of("Asia/Shanghai")),
            agenda = agenda,
            availableTools = registry.names(),
        )
    }

    private suspend fun ensureSystemPrompt() {
        val prompt = systemPromptText()
        if (history.firstOrNull()?.role == "system") history[0] = ChatMessage.system(prompt)
        else history.add(0, ChatMessage.system(prompt))
    }

    private suspend fun saveState() {
        val now = System.currentTimeMillis()
        val state = conversation.loadState()
        conversation.saveState(
            state.copy(
                msgCounter = store.rawCount(),
                round = state.round + 1,
                lastActivityAt = now,
                lastPlayerMessageAt = now,
            )
        )
    }

    private suspend fun compressIfNeeded() {
        if (!settings.configured) return
        val compressor = Compressor(
            store = store,
            llm = OpenAiClient(settings.baseUrl, settings.apiKey, settings.model),
            systemPrompt = systemPromptText(),
        )
        val outcomes = compressor.compressIfNeeded()
        val notable = outcomes.filter { it !is CompressionOutcome.Skipped }
        if (notable.isEmpty()) return

        reloadHistory()
        val text = notable.joinToString("；") { outcome ->
            when (outcome) {
                is CompressionOutcome.LeafCreated -> "把 ${outcome.messageCount} 条对话压成记忆节点 ${outcome.nodeId}"
                is CompressionOutcome.ParentCreated -> "把 ${outcome.childCount} 个节点上卷成 ${outcome.nodeId}"
                is CompressionOutcome.Failed -> "压缩失败（${outcome.reason}），原文保留"
                CompressionOutcome.Skipped -> ""
            }
        }
        _state.update { it.copy(memoryNotice = "记忆更新：$text") }
    }

    /** 压缩改写了 buffer → 从库里重建内存历史（system 保留在最前）。 */
    private suspend fun reloadHistory() {
        val system = history.firstOrNull { it.role == "system" }
        val restored = conversation.loadHistory()
        history.clear()
        system?.let { history += it }
        history += restored
    }

    private fun onEvent(assistantId: Long, event: AgentEvent) {
        when (event) {
            is AgentEvent.TextDelta -> patch(assistantId) { it.copy(text = it.text + event.text) }

            is AgentEvent.ToolStarted -> patch(assistantId) {
                it.copy(tools = it.tools + UiTool(name = event.name, args = event.args))
            }

            is AgentEvent.ToolFinished -> patch(assistantId) { msg ->
                val tools = msg.tools.toMutableList()
                val idx = tools.indexOfLast { it.name == event.name && it.result == null }
                if (idx >= 0) tools[idx] = tools[idx].copy(result = event.result)
                msg.copy(tools = tools)
            }

            is AgentEvent.Done -> patch(assistantId) {
                it.copy(text = it.text.ifBlank { "（这轮没有输出）" })
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
        val HHMM: java.time.format.DateTimeFormatter = java.time.format.DateTimeFormatter.ofPattern("HH:mm")
    }
}
