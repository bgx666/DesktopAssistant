package com.xiaozhu.planner.core.agent

import android.app.Application
import com.xiaozhu.planner.core.calendar.AgendaSnapshot
import com.xiaozhu.planner.core.calendar.CalendarDates
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.EndpointUrl
import com.xiaozhu.planner.core.llm.OpenAiClient
import com.xiaozhu.planner.core.memory.CompressionOutcome
import com.xiaozhu.planner.core.memory.Compressor
import com.xiaozhu.planner.core.memory.MemoryStore
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
import com.xiaozhu.planner.data.db.PlannerDatabase
import com.xiaozhu.planner.proactive.HeartbeatScheduler
import com.xiaozhu.planner.reminder.ReminderScheduler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * 「想一轮」这件事本身 —— 从 UI 里抽出来，因为**不止聊天页需要它**：
 * 心跳到点、提醒到点时也要跑一轮，而那些时候 App 可能根本没有界面（见 `HeartbeatService`）。
 *
 * 单例（每进程一个）是有意的：它持有对话历史与说话节奏。两个入口各持一份的话，
 * 后台跑过一轮、用户回到界面就看不到那段——记忆落在库里没错，但上下文会错位。
 *
 * 这里只管"想"：**不碰 UI、不朗读**。说话（TTS）是界面层的事。
 *
 * 并发：所有轮次串行（[roundMutex]）。同时跑两轮会写坏 buffer（assistant 的
 * tool_calls 与 tool 结果必须成对且相邻），所以宁可排队也不并发。
 */
class ConversationEngine private constructor(private val app: Application) {

    val settings = SettingsStore(app)
    /** 数据库单例（种子导入等场景要直接用它）。 */
    val db = PlannerDatabase.get(app)
    val store = MemoryStore(db)
    val calendar = CalendarStore(db.calendar())
    val conversation = ConversationService(store)
    val scheduler = ReminderScheduler(app, calendar, PrefsDndStore(app))

    /** 心跳闹钟（模型调 heartbeat 时由工具排上，见 HeartbeatPlanner）。 */
    val heartbeat = HeartbeatScheduler(app, PrefsHeartbeatStore(app))

    /** 下次醒来时留的备注（标题栏倒计时要带上它，与 PC 版一致）。 */
    fun heartbeatNote(): String = PrefsHeartbeatStore(app).current()?.second.orEmpty()

    /** `continue_speaking` 的说话节奏（跨轮沿用，跟 PC 版的 session 状态对齐）。 */
    val pacing = SpeechPacing()

    /** 工具清单在 [ToolRegistryFactory] 里（那边能被测试直接复用，不必搭起整个引擎）。 */
    val registry = ToolRegistryFactory.create(
        store = store,
        calendar = calendar,
        reminderPlanner = scheduler,
        heartbeatStore = PrefsHeartbeatStore(app),
        heartbeatPlanner = heartbeat,
        dndStore = PrefsDndStore(app),
        pacing = pacing,
    )

    /**
     * 对话历史（含 system）。**只有轮次会改它**，而轮次由 [roundMutex] 串行化，
     * 所以不需要额外加锁；外部一律别碰它（要读就读库）。
     */
    private val history = mutableListOf<ChatMessage>()
    private val roundMutex = Mutex()

    /** 后台压缩用的协程域（PC 版对应的是 `planner-compress` 线程）。 */
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** 压缩进行中（同一时间只压一次）。 */
    @Volatile
    private var compressing = false

    @Volatile
    private var loaded = false

    /** 最近一次压缩的结果（UI 用来说一句"记忆更新：…"；后台轮次没人看就算了）。 */
    @Volatile
    var lastCompression: List<CompressionOutcome> = emptyList()
        private set

    /** 正在想吗（心跳到点但界面正在生成时，就没必要再插一轮）。 */
    val busy: Boolean get() = roundMutex.isLocked

    /**
     * 恢复上次的对话。返回 true 表示确实恢复出了内容（UI 用来说一句"已恢复上次的对话"）。
     */
    suspend fun ensureLoaded(): Boolean {
        if (loaded) return false
        history += conversation.loadHistory()
        loaded = true
        val state = conversation.loadState()
        return state.round > 0 || state.msgCounter > 0
    }

    /**
     * 建模型客户端。**只此一处**：URL 归一化与附加请求头（opencode 的 session）
     * 必须每条路径一致——对话、压缩各建一份的话，很容易只修了其中一处。
     */
    fun llmClient(): OpenAiClient = OpenAiClient(
        baseUrl = settings.baseUrl,
        apiKey = settings.apiKey,
        model = settings.model,
        extraHeaders = EndpointUrl.extraHeaders(settings.baseUrl, settings.openCodeSession),
        userAgent = EndpointUrl.userAgent(com.xiaozhu.planner.BuildConfig.VERSION_NAME),
    )

    /**
     * 跑一轮：把 [message] 追加进历史、落盘、把事件交给 [onEvent]，结束后落盘 + 压缩 + 重排闹钟。
     *
     * 用回调而不是返回 Flow：整轮（含工具往返）必须在一把锁里完成，
     * 而 Flow 是"交给调用方去 collect"的，锁没法跨过 collect 保住。
     */
    suspend fun runRound(message: ChatMessage, onEvent: suspend (AgentEvent) -> Unit) {
        roundMutex.withLock {
            ensureLoaded()
            refreshSystemPrompt()

            // 先落盘：中途被杀也不至于把用户刚说的话弄丢。
            // before 必须在这之后取，否则这条会被 persist 两次（恢复时看到两份）。
            history += message
            val before = history.size
            runCatching { conversation.persist(listOf(message), System.currentTimeMillis()) }

            val loop = AgentLoop(llmClient(), registry, pacing = pacing)
            try {
                // 直接把 history 交给 loop：它会把 assistant(tool_calls) 与 tool 结果成对追加进去，
                // 下一轮就带着这些上下文再问模型（传副本会让上下文断在这里）
                loop.run(history).collect { onEvent(it) }
            } finally {
                finishRound(before)
            }
        }
        // 锁**外**触发压缩：下一次对话不必等这次压缩（PC 版就是后台线程做的）
        startCompression()
    }

    /** 一轮结束后的收尾（放在 finally 里：中途取消也要把已生成的部分落盘）。 */
    private suspend fun finishRound(before: Int) {
        val tail = if (history.size <= before) emptyList() else history.drop(before).toList()
        runCatching { conversation.persist(tail, System.currentTimeMillis()) }
        runCatching { saveState() }
        // 模型可能刚改过日程（工具内部也会 sync，这里兜一层：重复调用是幂等的）
        runCatching { scheduler.sync() }
        // 注意：**压缩不在这里做**。PC 版是另起线程压，只有"换入"时才等空闲窗口；
        // 放在轮次里会让下一次对话一直等这次压缩（用户体感：说话前先卡几秒）。
    }

    /**
     * 压缩（后台跑，**不阻塞对话**）。
     *
     * 与 PC 版 `_maybe_compress_async` 同构：模型调用慢，但对话不该等它。
     * 换入（改 buffer + 重建内存历史）时才短暂拿一次 [roundMutex]。
     */
    private fun startCompression() {
        if (compressing) return
        compressing = true
        scope.launch {
            try {
                val outcomes = compressIfNeeded()
                lastCompression = outcomes
                if (outcomes.any { it !is CompressionOutcome.Skipped }) {
                    // 只在换入这一刻占用锁：此刻 buffer 已经改完，重建内存历史即可
                    roundMutex.withLock { reloadHistory() }
                    _compressionEvents.emit(outcomes)
                }
            } catch (_: Exception) {
                // 压缩失败绝不能影响对话
            } finally {
                compressing = false
            }
        }
    }

    /** 压缩完成的广播（界面据此提示"记忆更新：…"并重画记忆卡片）。 */
    private val _compressionEvents = MutableSharedFlow<List<CompressionOutcome>>(extraBufferCapacity = 4)
    val compressionEvents: SharedFlow<List<CompressionOutcome>> = _compressionEvents

    /**
     * 撤销：把第 [rawIndex] 条**原始消息及其之后**的全部原文删掉（PC 版 `/undo` 同义）。
     *
     * 只动"原文"，**不动记忆节点**——那些内容已经压进长期记忆树了，
     * 删掉它等于删记忆，而不是"撤回刚说的一句话"。
     *
     * 返回是否真的改了东西（界面据此决定要不要把内容放回输入框）。
     */
    suspend fun truncateFromRaw(rawIndex: Int): Boolean = roundMutex.withLock {
        if (!conversation.truncateFrom(rawIndex)) return@withLock false
        // 内存里的历史要跟着回退，否则下一轮会把已经删掉的内容再发一遍
        history.clear()
        loaded = false
        ensureLoaded()
        true
    }

    /** system prompt 每轮重建：日期、议程、可用工具清单都要是最新的。 */
    suspend fun systemPromptText(): String {
        val today = CalendarDates.todayText()
        val agenda = runCatching {
            AgendaSnapshot.render(
                today = today,
                items = calendar.range(today, CalendarDates.plusDays(today, AgendaSnapshot.WINDOW_DAYS) ?: today),
            )
        }.getOrNull()
        return SystemPrompt.build(
            template = app.assets.open("prompts/system.md").bufferedReader().use { it.readText() },
            now = ZonedDateTime.now(ZoneId.of("Asia/Shanghai")),
            agenda = agenda,
            availableTools = registry.names(),
        )
    }

    private suspend fun refreshSystemPrompt() {
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

    private suspend fun compressIfNeeded(): List<CompressionOutcome> {
        if (!settings.configured) {
            lastCompression = emptyList()
            return emptyList()
        }
        val compressor = Compressor(
            store = store,
            llm = llmClient(),
            systemPrompt = systemPromptText(),
        )
        val outcomes = compressor.compressIfNeeded()
        lastCompression = outcomes
        if (outcomes.any { it !is CompressionOutcome.Skipped }) reloadHistory()
        return outcomes
    }

    /** 压缩改写了 buffer → 从库里重建内存历史（system 保留在最前）。 */
    private suspend fun reloadHistory() {
        val system = history.firstOrNull { it.role == "system" }
        val restored = conversation.loadHistory()
        history.clear()
        system?.let { history += it }
        history += restored
    }

    companion object {
        @Volatile
        private var instance: ConversationEngine? = null

        fun get(app: Application): ConversationEngine =
            instance ?: synchronized(this) {
                instance ?: ConversationEngine(app).also { instance = it }
            }
    }
}
