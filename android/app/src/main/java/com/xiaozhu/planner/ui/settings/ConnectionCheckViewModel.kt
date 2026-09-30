package com.xiaozhu.planner.ui.settings

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.EndpointUrl
import com.xiaozhu.planner.core.llm.OpenAiClient
import com.xiaozhu.planner.data.SettingsStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 「测试连接」：发一条最小请求，把**实际发出去的地址与请求头**连同响应一起摆出来。
 *
 * 为什么值得做：模型服务这一层出问题时，用户能看到的只有一行 `HTTP 400 {...}`，
 * 于是每次都要来回猜——"你填的 base 是什么""装的是哪一版""头发了没"。
 * 一次点击就能把这三件事全部回答掉：
 *
 * - 地址：把归一化后的真实 URL 打出来（这次的 404 就是路径重复）
 * - 请求头：列出真发了哪些（这次的 400 就是少一个 session 头）
 * - 响应：HTTP 码 + 正文前几百字（网关的错误说明通常直接可读）
 */
data class ConnectionCheckState(
    val running: Boolean = false,
    val ok: Boolean? = null,
    val detail: String? = null,
)

class ConnectionCheckViewModel(app: Application) : AndroidViewModel(app) {

    private val settings = SettingsStore(app)

    private val _state = MutableStateFlow(ConnectionCheckState())
    val state: StateFlow<ConnectionCheckState> = _state.asStateFlow()

    fun run() {
        if (_state.value.running) return
        viewModelScope.launch {
            _state.value = ConnectionCheckState(running = true)
            val result = withContext(Dispatchers.IO) { check() }
            _state.value = result
        }
    }

    private suspend fun check(): ConnectionCheckState {
        if (!settings.configured) {
            return ConnectionCheckState(ok = false, detail = "还没填 API Key。")
        }
        val client = OpenAiClient(
            baseUrl = settings.baseUrl,
            apiKey = settings.apiKey,
            model = settings.model,
            extraHeaders = EndpointUrl.extraHeaders(settings.baseUrl, settings.openCodeSession),
            userAgent = EndpointUrl.userAgent(com.xiaozhu.planner.BuildConfig.VERSION_NAME),
        )

        val header = buildString {
            append("地址：").append(client.endpoint()).append('\n')
            append("模型：").append(settings.model).append('\n')
            append("请求头：").append(client.sentHeaders().joinToString("、"))
            if (EndpointUrl.isOpenCode(settings.baseUrl)) {
                append("\n（opencode 网关要求 ").append(EndpointUrl.SESSION_HEADER)
                append("，已带；没有它会 400 MissingSessionID）")
            }
        }

        return try {
            // 用 complete（非流式、只要一个字）：最省的一次真实请求
            val reply = client.complete(listOf(ChatMessage.user("只回一个字：好")))
            ConnectionCheckState(
                ok = true,
                detail = header + "\n\n✓ 通了。模型回了：" + reply.trim().take(60),
            )
        } catch (e: Exception) {
            ConnectionCheckState(
                ok = false,
                detail = header + "\n\n✗ " + (e.message ?: e.javaClass.simpleName).take(600),
            )
        }
    }
}
