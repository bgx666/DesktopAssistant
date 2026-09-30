package com.xiaozhu.planner.core.speech

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.Base64
import java.util.concurrent.TimeUnit

/**
 * 小米 MiMo TTS（OpenAI **兼容**，但注意它走的是 `/chat/completions` 而不是
 * `/audio/speech`：把要读的文本放在 assistant 消息里，音频以 base64 回在
 * `choices[0].message.audio.data`）。参数与 PC 版 `tts.py::_synthesize_mimo` 一致。
 *
 * 为什么在手机上还要用云 TTS 而不是系统自带的：音色与 PC 版统一（同一把嗓子），
 * 而且系统 TTS 的中文引擎在不少 ROM 上要用户自己去装。系统 TTS 作为**兜底**
 * 保留（见 `AndroidTtsSpeaker`）——没配 key 或断网时也不至于哑巴。
 */
class MiMoTts(
    private val apiKey: String,
    private val baseUrl: String = DEFAULT_BASE,
    private val model: String = DEFAULT_MODEL,
    private val voice: String = DEFAULT_VOICE,
    private val http: OkHttpClient = defaultClient(),
) {

    val available: Boolean get() = apiKey.isNotBlank()

    /** 合成一段语音；失败返回 null（调用方决定要不要退回系统 TTS）。 */
    fun synthesize(text: String, voiceOverride: String? = null): ByteArray? {
        val speech = SpeechText.clean(text)
        if (speech.isBlank()) return null

        val payload = buildJsonObject {
            put("model", model)
            putJsonArray("messages") {
                add(buildJsonObject {
                    put("role", "user")
                    put("content", "请用自然、亲切、清晰的中文语气朗读。")
                })
                add(buildJsonObject {
                    put("role", "assistant")
                    put("content", speech)
                })
            }
            putJsonObject("audio") {
                put("format", "wav")
                put("voice", (voiceOverride ?: voice).ifBlank { DEFAULT_VOICE })
            }
        }

        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/chat/completions")
            .addHeader("api-key", apiKey)
            .addHeader("Content-Type", "application/json")
            .post(payload.toString().toRequestBody(JSON))
            .build()

        return runCatching {
            http.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return null
                val body = response.body?.string() ?: return null
                val data = MiMoResponse.audioBase64(body) ?: return null
                Base64.getDecoder().decode(data)
            }
        }.getOrNull()
    }

    companion object {
        const val DEFAULT_BASE = "https://api.xiaomimimo.com/v1"
        const val DEFAULT_MODEL = "mimo-v2.5-tts"
        const val DEFAULT_VOICE = "mimo_default"

        private val JSON = "application/json; charset=utf-8".toMediaType()

        /** MiMo 预置音色（官方文档 2026-08 版本，与 PC 版同一份清单）。 */
        val VOICES = listOf(
            "mimo_default" to "MiMo 默认（中国集群＝冰糖）",
            "冰糖" to "冰糖 · 中文女声",
            "茉莉" to "茉莉 · 中文女声",
            "苏打" to "苏打 · 中文男声",
            "白桦" to "白桦 · 中文男声",
            "Mia" to "Mia · 英文女声",
            "Chloe" to "Chloe · 英文女声",
            "Milo" to "Milo · 英文男声",
            "Dean" to "Dean · 英文男声",
        )

        private fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)      // 合成比对话慢，60 秒与 PC 版一致
            .writeTimeout(30, TimeUnit.SECONDS)
            .build()
    }
}
