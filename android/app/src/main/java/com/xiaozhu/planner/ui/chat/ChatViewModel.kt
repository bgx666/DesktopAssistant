package com.xiaozhu.planner.ui.chat

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.agent.AgentEvent
import com.xiaozhu.planner.core.agent.AgentLoop
import com.xiaozhu.planner.core.agent.SystemPrompt
import com.xiaozhu.planner.core.agent.ToolRegistry
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.OpenAiClient
import com.xiaozhu.planner.core.tools.HeartbeatTool
import com.xiaozhu.planner.core.tools.WebFetchTool
import com.xiaozhu.planner.core.tools.WebSearchTool
import com.xiaozhu.planner.data.PrefsHeartbeatStore
import com.xiaozhu.planner.data.SettingsStore
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * P0 的会话中枢：设置 → 客户端 → agent 循环 → UI 状态。
 *
 * 注意：P0 的对话**只存在内存里**（重启即清空）——持久化是 P1（Room + 记忆树）的事。
 * 这里刻意不碰存储，避免先做一个马上要被替换掉的半成品。
 */
class ChatViewModel(app: Application) : AndroidViewModel(app) {

    private val settings = SettingsStore(app)

    private val registry = ToolRegistry(
        listOf(
            WebSearchTool(),
            WebFetchTool(),
            HeartbeatTool(PrefsHeartbeatStore(app)),
        )
    )

    private val history = mutableListOf<ChatMessage>()
    private var running: Job? = null
    private var nextId = 1L

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    fun refreshSettings() {
        _state.update { it.copy(configured = settings.configured, hint = null) }
    }

    fun send(text: String) {
        val body = text.trim()
        if (body.isEmpty() || _state.value.streaming) return

        if (!settings.configured) {
            _state.update { it.copy(hint = "还没配置 API Key，先去设置页填一个。") }
            return
        }

        appendUser(body)
        val assistantId = nextId++
        _state.update {
            it.copy(
                messages = it.messages + UiMessage(id = assistantId, fromUser = false),
                streaming = true,
                hint = null,
            )
        }

        running = viewModelScope.launch {
            ensureSystemPrompt()
            val loop = AgentLoop(
                OpenAiClient(settings.baseUrl, settings.apiKey, settings.model),
                registry,
            )
            try {
                loop.run(history).collect { event -> onEvent(assistantId, event) }
            } catch (e: Exception) {
                patch(assistantId) { it.copy(text = it.text + "\n（中断：${e.message}）") }
            } finally {
                _state.update { it.copy(streaming = false) }
            }
        }
    }

    fun stop() {
        running?.cancel()
        running = null
        _state.update { it.copy(streaming = false) }
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
                val text = it.text.ifBlank { "（这轮没有输出）" }
                it.copy(text = text)
            }

            is AgentEvent.Error -> patch(assistantId) {
                it.copy(text = (it.text + "\n出错了：" + event.message).trim(), failed = true)
            }
        }
    }

    private fun appendUser(text: String) {
        history += ChatMessage.user(text)
        _state.update { it.copy(messages = it.messages + UiMessage(id = nextId++, fromUser = true, text = text)) }
    }

    /** system prompt 每轮重建：日期、可用工具清单都要是最新的。 */
    private fun ensureSystemPrompt() {
        val template = getApplication<Application>().assets
            .open("prompts/system.md").bufferedReader().use { it.readText() }
        val prompt = SystemPrompt.build(
            template = template,
            now = ZonedDateTime.now(ZoneId.of("Asia/Shanghai")),
            agenda = null, // P2 接入日历后填 [当前议程]
            availableTools = registry.names(),
        )
        if (history.firstOrNull()?.role == "system") history[0] = ChatMessage.system(prompt)
        else history.add(0, ChatMessage.system(prompt))
    }

    private fun patch(id: Long, transform: (UiMessage) -> UiMessage) {
        _state.update { s ->
            s.copy(messages = s.messages.map { if (it.id == id) transform(it) else it })
        }
    }
}