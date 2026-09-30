package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.llm.FunctionSpec
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.llm.ToolCall
import com.xiaozhu.planner.core.llm.ToolSpec
import kotlinx.serialization.json.JsonObject

/**
 * 工具注册表：把 [Tool] 变成模型能看到的 schema，并把模型的调用变成一次执行。
 *
 * 三条纪律（PC 版的教训）：
 * 1. **调用不存在的工具**不许静默——返回一句话告诉模型有哪些可用；
 * 2. **参数不是合法 JSON** 不许抛——返回可读错误，让模型自己改；
 * 3. **工具内部异常**不许炸掉整轮——转成文本结果，模型可以换个方式再试。
 */
class ToolRegistry(private val tools: List<Tool>) {

    private val byName = tools.associateBy { it.name }

    fun specs(): List<ToolSpec> = tools.map {
        ToolSpec(function = FunctionSpec(it.name, it.description, it.parameters))
    }

    fun names(): List<String> = tools.map { it.name }

    suspend fun execute(call: ToolCall): String {
        val tool = byName[call.function.name]
            ?: return "没有名为 ${call.function.name} 的工具。本轮可用的工具：${names().joinToString("、")}。"

        val args = try {
            (PlannerJson.parseToJsonElement(call.function.arguments.ifBlank { "{}" }) as? JsonObject)
                ?: JsonObject(emptyMap())
        } catch (_: Exception) {
            return "工具 ${tool.name} 的参数不是合法 JSON，请重新给出：${call.function.arguments.take(200)}"
        }

        return try {
            tool.run(args)
        } catch (e: Exception) {
            "工具 ${tool.name} 执行失败：${e.javaClass.simpleName}: ${e.message ?: "未知错误"}"
        }
    }
}