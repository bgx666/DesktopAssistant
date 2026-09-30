package com.xiaozhu.planner.core.agent

import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/**
 * 组装最终 system prompt：模板（assets/prompts/system.md）+ 运行时事实。
 *
 * 纯函数，方便单测。**可用工具那段是重点**：它由注册表实际内容生成，
 * 所以模型永远不会被 prompt 承诺一个当前版本还没有的工具（也就不会假装"记上了"）。
 */
object SystemPrompt {

    private val STAMP = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm (EEEE)")

    fun build(
        template: String,
        now: ZonedDateTime,
        agenda: String? = null,
        availableTools: List<String> = emptyList(),
    ): String {
        val sb = StringBuilder(template.trimEnd())
        sb.append("\n\n## 运行时事实（系统注入，不是用户在说话）\n\n")
        sb.append("- 现在是 ").append(now.format(STAMP)).append("，北京时间 UTC+8。\n")

        if (!agenda.isNullOrBlank()) {
            sb.append('\n').append(agenda.trim()).append('\n')
        }

        if (availableTools.isNotEmpty()) {
            sb.append("\n本轮可用工具：").append(availableTools.joinToString("、")).append("。\n")
            sb.append("**没出现在这个列表里的能力，当前版本还没接入。**")
            sb.append("用户让你做这类事（比如记日程、翻旧账）时，如实说现在还做不到，")
            sb.append("不要假装已经完成，也不要说\"我记上了\"。\n")
        }
        return sb.toString()
    }
}