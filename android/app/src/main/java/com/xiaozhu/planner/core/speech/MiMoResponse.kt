package com.xiaozhu.planner.core.speech

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * MiMo 返回体的解析。抽出来是为了能**不看网络**就测：真实响应嵌套四层
 * （`choices[0].message.audio.data`），字段名或层级记错时表现为"静默没声音"，
 * 那是很难查的一类故障。
 */
object MiMoResponse {

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** 取 `choices[0].message.audio.data`（base64）；结构不对返回 null，不抛。 */
    fun audioBase64(body: String): String? = runCatching {
        val root = json.parseToJsonElement(body).jsonObject
        val choices = root["choices"]?.jsonArray ?: return null
        val message = choices.firstOrNull()?.jsonObject?.get("message")?.jsonObject ?: return null
        val audio = message["audio"]?.jsonObject ?: return null
        audio["data"]?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }
    }.getOrNull()

    /** 出错时的说明（`error.message`），用于日志/提示。 */
    fun errorMessage(body: String): String? = runCatching {
        json.parseToJsonElement(body).jsonObject["error"]?.jsonObject
            ?.get("message")?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }
    }.getOrNull()
}
