package com.xiaozhu.planner.core.agent

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * 一个模型可调用的工具。实现只做「解析参数 → 干活 → 返回文本」；
 * 鉴权、异常兜底、UI 反馈都在 registry / loop / UI 层。
 */
interface Tool {
    val name: String
    val description: String

    /** JSON Schema（object 根）。手机上的工具参数都简单，手写即可。 */
    val parameters: JsonObject

    suspend fun run(args: JsonObject): String
}

// ── 参数读取小工具（模型经常给错类型，这里统一容错）──

fun JsonObject.str(key: String): String? =
    (this[key] as? JsonPrimitive)?.content?.takeIf { it.isNotBlank() }

fun JsonObject.int(key: String, default: Int): Int =
    (this[key] as? JsonPrimitive)?.content?.trim()?.toIntOrNull() ?: default

fun JsonObject.long(key: String, default: Long): Long =
    (this[key] as? JsonPrimitive)?.content?.trim()?.toLongOrNull() ?: default

fun JsonObject.double(key: String): Double? =
    (this[key] as? JsonPrimitive)?.content?.trim()?.toDoubleOrNull()

/**
 * 布尔参数。模型经常把 true 写成 `"true"` / `"是"` 甚至 `1`，
 * `jsonPrimitive.boolean` 遇到这些会直接抛异常——这里全部吃掉，落到默认值。
 */
fun JsonObject.bool(key: String, default: Boolean = false): Boolean {
    val raw = (this[key] as? JsonPrimitive)?.content?.trim()?.lowercase() ?: return default
    return when (raw) {
        "true", "1", "yes", "y", "是" -> true
        "false", "0", "no", "n", "否", "" -> false
        else -> default
    }
}