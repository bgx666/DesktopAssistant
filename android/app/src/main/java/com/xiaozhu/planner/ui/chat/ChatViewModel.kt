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
import com.xiaozhu.planner.core.memory.NodeInfo
import com.xiaozhu.planner.core.speech.Speaker
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

/**
 * 聊天页的中枢：把 UI 事件翻译成引擎调用，把引擎事件画到界面上。
 *
 * 分工（重构后）：
 * - **"想一轮"在 [ConversationEngine]**（单例）——心跳/提醒在后台也要用它，所以它不能长在 UI 里
 * - 这里只管：上屏、朗读、提示、记忆浏览入口
 * - **Room 是 buffer 的唯一事实源**；引擎内存里的历史只是它的视图（压缩后重建）
 * - 压缩失败**绝不影响这一轮对话**（对话优先，记忆是渐进增强）
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
        // 冷启动就要知道"配没配 key"，否则用户第一次发消息才被告知没配置
        refreshSettings()
        // 界面活着 → 心跳到点由这里说（后台时由 HeartbeatService 自己跑）
        AppPresence.uiAlive = true
        // 提醒到点 / 心跳到点 → 让模型说一句。
        // App 正开着才收得到这些事件；没开时后台服务会自己跑一轮并推通知。
        viewModelScope.launch {
            ReminderEvents.fired.collect { lines -> onReminderFired(lines) }
        }
        viewModelScope.launch {
            HeartbeatEvents.fired.collect { note -> onHeartbeatFired(note) }
        }
        // 启动时收拾一次：续期重复系列 + 重排闹钟 + 把心跳闹钟补上
        // （重启后 AlarmManager 是空的，而心跳时间存在 Prefs 里）
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { engine.calendar.extendAllSeries() }
            runCatching { engine.scheduler.sync() }
            runCatching { engine.heartbeat.arm() }
        }
    }

    override fun onCleared() {
        AppPresence.uiAlive = false
        super.onCleared()
    }

    fun refreshSettings() {
        _state.update { it.copy(configured = engine.settings.configured, hint = null) }
    }

    /** 给 UI 层显示一行提示（语音识别失败等）。 */
    fun showHint(text: String) {
        _state.update { it.copy(hint = text) }
    }

    // ── 对话 ────────────────────────────────────────────────

    fun send(text: String) {
        val body = text.trim()
        if (body.isEmpty()) return
        // 用户开口就闭嘴：听着长回复想插话时，最烦的是"它还在念"
        speaker.stop()
        submit(ChatMessage.user(body), fromUser = true, display = body)
    }

    /** 手动朗读某条回复（自动朗读关掉时用）。 */
    fun speak(text: String) = speaker.speak(text)

    fun stopSpeaking() = speaker.stop()

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
        val text = ProactivePrompts.reminder(CalendarDates.now().format(HHMM), lines)
        submit(ChatMessage.user(text), fromUser = false, display = lines.joinToString("\n"), fromSystem = true)
    }

    /** 心跳到点：界面活着就在这里说，界面不在时由 HeartbeatService 推通知。 */
    private fun onHeartbeatFired(note: String) {
        val text = ProactivePrompts.heartbeat(CalendarDates.now().format(HHMM), note)
        submit(
            ChatMessage.user(text),
            fromUser = false,
            display = note.ifBlank { "（我醒了一下）" },
            fromSystem = true,
        )
    }

    private fun submit(
        message: ChatMessage,
        fromUser: Boolean,
        display: String,
        fromSystem: Boolean = false,
    ) {
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
                    UiMessage(id = userId, fromUser = fromUser, fromSystem = fromSystem, text = display) +
                    UiMessage(id = assistantId, fromUser = false),
                streaming = true,
                hint = null,
                memoryNotice = null,
            )
        }

        running = viewModelScope.launch {
            val firstRound = runCatching { engine.ensureLoaded() }.getOrDefault(false)
            if (firstRound) {
                _state.update {
                    it.copy(memoryNotice = "已恢复上次的对话与记忆（原文 ${engine.store.rawCount()} 条）")
                }
            }
            try {
                engine.runRound(message) { event -> onEvent(assistantId, event) }
            } catch (e: Exception) {
                patch(assistantId) { it.copy(text = it.text + "\n（中断：${e.message}）") }
            } finally {
                _state.update { it.copy(streaming = false) }
            }
            // 压缩是引擎内部做的（后台轮次也要压），结果回读一下说给用户听
            describeCompression(engine.lastCompression)?.let { notice ->
                _state.update { it.copy(memoryNotice = "记忆更新：$notice") }
            }
        }
    }

    // ── 记忆 ────────────────────────────────────────────────

    suspend fun memoryStats(): MemoryStats = withContext(Dispatchers.IO) {
        val store = engine.store
        val levels = (0..4).map { it to store.levelCount(it) }.filter { it.second > 0 }
        MemoryStats(
            totalNodes = store.nodeCount(),
            levels = levels,
            rootId = store.rootId(),
            rawMessages = store.rawCount(),
        )
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

    // ── 事件 → UI ───────────────────────────────────────────

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
                val final = it.text.ifBlank { "（这轮没有输出）" }
                // 朗读在事件处理里发起：此时文本已经完整，且不阻塞 UI 线程
                speaker.speak(final)
                it.copy(text = final)
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
