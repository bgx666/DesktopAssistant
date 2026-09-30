package com.xiaozhu.planner.core.memory

/**
 * 压缩输出的解析（三级降级，与 PC 版一致）：
 * 1. 直接按 JSON 解析；
 * 2. 剥掉 ```json 围栏 / 前后废话后再解析（模型常常多嘴）；
 * 3. 都失败就退化成"整段文本当 summary"——宁可只拿到摘要，也不要丢掉这段记忆。
 *
 * 纯函数，可单测。
 */
object NodeOutputParser {

    fun parse(raw: String): MemoryNodeOutput? {
        val text = raw.trim()
        if (text.isEmpty()) return null

        direct(text)?.let { return it }

        extractJsonObject(text)?.let { candidate ->
            direct(candidate)?.let { return it }
        }

        // 兜底：整段当摘要（但要保证不是空话/纯围栏）
        val plain = stripFence(text).trim()
        return if (plain.isEmpty()) null else MemoryNodeOutput(summary = plain)
    }

    private fun direct(text: String): MemoryNodeOutput? =
        runCatching { MemoryJson.json.decodeFromString(MemoryNodeOutput.serializer(), text) }
            .getOrNull()
            ?.takeIf { it.summary.isNotBlank() || !it.profile.isEmpty }

    /** 从一堆文字里抠出第一个平衡的 {...}（处理模型在 JSON 前后写解释的情况）。 */
    fun extractJsonObject(text: String): String? {
        val start = text.indexOf('{')
        if (start < 0) return null
        var depth = 0
        var inString = false
        var escaped = false
        for (i in start until text.length) {
            val c = text[i]
            when {
                escaped -> escaped = false
                c == '\\' && inString -> escaped = true
                c == '"' -> inString = !inString
                !inString && c == '{' -> depth++
                !inString && c == '}' -> {
                    depth--
                    if (depth == 0) return text.substring(start, i + 1)
                }
            }
        }
        return null
    }

    fun stripFence(text: String): String {
        var s = text.trim()
        if (s.startsWith("```")) {
            val nl = s.indexOf('\n')
            if (nl > 0) s = s.substring(nl + 1)
            if (s.trimEnd().endsWith("```")) s = s.trimEnd().dropLast(3)
        }
        return s.trim()
    }
}