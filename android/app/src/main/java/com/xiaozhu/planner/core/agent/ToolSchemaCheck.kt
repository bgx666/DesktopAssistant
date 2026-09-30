package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.llm.ToolSpec
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * 工具 schema 体检：**不联网**就能发现"网关会拒掉整个请求"的那种错误。
 *
 * 起因是一次真实故障：`continue_speaking` 的 `properties` 被写成了**字符串**
 * （本意是给个说明），而 JSON Schema 要求它是对象。一个工具非法 → 网关把
 * **整个请求**拒掉，而且只回一句 `{"model":"deepseek-v4.1-flash"}`——
 * 完全看不出是哪个工具的问题，排查了很久。
 *
 * 所以把"合法 JSON Schema 的最小要求"固化成断言：这类错误在单测里就红。
 */
object ToolSchemaCheck {

    /** 返回问题列表（空 = 合法）。 */
    fun problems(spec: ToolSpec): List<String> {
        val name = spec.function.name
        val out = mutableListOf<String>()
        val params = spec.function.parameters

        if (spec.type != "function") out += "$name: type 必须是 function，实际 ${spec.type}"
        if (spec.function.description.isBlank()) out += "$name: description 不能为空"

        when (val type = params["type"]) {
            null -> out += "$name: parameters 缺少 type"
            is JsonPrimitive -> if (type.content != "object") {
                out += "$name: parameters.type 必须是 object，实际 ${type.content}"
            }
            else -> out += "$name: parameters.type 不是字符串"
        }

        when (val props = params["properties"]) {
            null -> out += "$name: parameters 缺少 properties（无参数也要给空对象 {}）"
            is JsonObject -> props.forEach { (key, value) ->
                val schema = value as? JsonObject
                    ?: run { out += "$name.$key: 属性定义必须是对象"; return@forEach }
                if (schema["type"] !is JsonPrimitive) {
                    out += "$name.$key: 属性缺少 type"
                }
            }
            // 这一条就是踩过的坑：properties 写成字符串
            else -> out += "$name: parameters.properties 必须是对象，实际是 " +
                (props::class.simpleName ?: "未知类型")
        }

        when (val required = params["required"]) {
            null -> Unit
            is JsonArray -> required.forEach { item ->
                val key = (item as? JsonPrimitive)?.content
                if (key.isNullOrBlank()) {
                    out += "$name: required 里有过空项"
                } else if ((params["properties"] as? JsonObject)?.containsKey(key) != true) {
                    out += "$name: required 里的 $key 不在 properties 里"
                }
            }
            else -> out += "$name: required 必须是数组"
        }

        return out
    }

    fun problems(specs: List<ToolSpec>): List<String> = specs.flatMap { problems(it) }
}
