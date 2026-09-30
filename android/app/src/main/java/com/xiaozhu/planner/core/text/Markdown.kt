package com.xiaozhu.planner.core.text

/**
 * 轻量 Markdown 渲染 —— **逐条对照 PC 版 `frontend/renderer/md.js`**。
 *
 * PC 版只支持三种标记（刻意不支持完整 Markdown）：
 * 1. 代码块 ```…```（先占位，避免内容被后面的加粗规则误伤）
 * 2. 行内代码 `…`
 * 3. 加粗 `**…**`
 *
 * 在 Compose 里不需要 escapeHtml（没有 HTML 注入面），但要保留同样的
 * **顺序与边界**：行内代码里的 `**` 不该变成加粗、代码块里的任何标记都不解析。
 * 输出用 [MarkdownSpan] 表示，交给 UI 转成 AnnotatedString——这样这层逻辑是纯的、可测的。
 */
object Markdown {

    /** 一段文本的样式标记。 */
    enum class Style { Plain, Bold, Code, CodeBlock }

    data class MarkdownSpan(val text: String, val style: Style)

    private const val PLACEHOLDER = '\u0000'

    /**
     * 解析成若干段。**不丢字**：所有原文都会出现在某一 [MarkdownSpan] 里
     * （渲染成 HTML 时丢了字看不出来，这里丢了字就是消息内容少了）。
     */
    fun parse(text: String?): List<MarkdownSpan> {
        if (text.isNullOrEmpty()) return emptyList()

        // 1) 代码块先抠出来占位（与 md.js 同样的理由：别让内部标记被后续规则吃掉）
        val blocks = mutableListOf<String>()
        var work = Regex("```([\\s\\S]*?)```").replace(text) { m ->
            blocks += m.groupValues[1].trim()
            "$PLACEHOLDER${blocks.size - 1}$PLACEHOLDER"
        }

        val spans = mutableListOf<MarkdownSpan>()
        // 2) 行内代码与加粗：一次扫描，谁先出现先切谁（避免互相嵌套误伤）
        val inline = Regex("`([^`\\n]+)`|\\*\\*([^*\\n]+)\\*\\*|${PLACEHOLDER}(\\d+)${PLACEHOLDER}")
        var cursor = 0
        inline.findAll(work).forEach { m ->
            if (m.range.first > cursor) {
                spans += MarkdownSpan(work.substring(cursor, m.range.first), Style.Plain)
            }
            val code = m.groups[1]?.value
            val bold = m.groups[2]?.value
            val blockIndex = m.groups[3]?.value?.toIntOrNull()
            when {
                code != null -> spans += MarkdownSpan(code, Style.Code)
                bold != null -> spans += MarkdownSpan(bold, Style.Bold)
                blockIndex != null && blockIndex in blocks.indices ->
                    spans += MarkdownSpan(blocks[blockIndex], Style.CodeBlock)
                else -> spans += MarkdownSpan(m.value, Style.Plain)
            }
            cursor = m.range.last + 1
        }
        if (cursor < work.length) {
            spans += MarkdownSpan(work.substring(cursor), Style.Plain)
        }
        return spans.filter { it.text.isNotEmpty() }
    }

    /** 纯文本版（朗读、通知、卡片摘要用）：把标记去掉，保留内容。 */
    fun toPlainText(text: String?): String =
        parse(text).joinToString("") { it.text }

    /**
     * 中文工具名（PC 版 `TOOL_DISPLAY`）。
     *
     * 只翻"光看英文名不知道在干什么"的那些；其余保持原名——模型与日志里
     * 都是英文名，乱翻反而对不上。
     */
    private val TOOL_DISPLAY = mapOf(
        "heartbeat" to "设置定时唤醒",
        "continue_speaking" to "继续说下一点",
        "set_do_not_disturb" to "免打扰",
        "list_schedule" to "查看日程",
        "add_schedule" to "加入日历",
        "update_schedule" to "修改日程",
        "cancel_schedule" to "取消日程",
        "explore_memory_tree" to "翻阅记忆",
        "search_memory" to "搜索记忆",
        "web_search" to "搜索网页",
        "fetch_web" to "读取网页",
    )

    fun toolDisplayName(name: String): String = TOOL_DISPLAY[name] ?: name
}
