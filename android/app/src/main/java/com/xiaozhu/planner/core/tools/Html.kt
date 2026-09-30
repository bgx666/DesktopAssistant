package com.xiaozhu.planner.core.tools

/**
 * 极简 HTML → 纯文本 / 链接提取（纯 Kotlin，可单测）。
 *
 * 为什么不引第三方解析库：这一步只需要「去掉标签、保留可读文本」，
 * 而且工具结果最终是喂给模型的，不需要严谨的 DOM 语义（PC 版同思路）。
 */
object Html {

    private val SCRIPT = Regex("(?is)<script.*?</script>")
    private val STYLE = Regex("(?is)<style.*?</style>")
    private val COMMENT = Regex("(?s)<!--.*?-->")
    private val BLOCK_TAGS = Regex(
        "(?i)</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|table|ul|ol|blockquote)[^>]*>"
    )
    private val TAG = Regex("(?s)<[^>]+>")
    private val BLANK_LINES = Regex("\n{3,}")
    private val SPACES = Regex("[ \\t\\x0B\\f\\r]{2,}")
    private val LINK = Regex("(?is)<a\\s+[^>]*href\\s*=\\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>")

    fun toPlainText(html: String, maxChars: Int): String {
        var s = html
        s = COMMENT.replace(s, " ")
        s = SCRIPT.replace(s, " ")
        s = STYLE.replace(s, " ")
        s = BLOCK_TAGS.replace(s, "\n")
        s = TAG.replace(s, "")
        s = decodeEntities(s)
        s = SPACES.replace(s, " ")
        s = s.lines().joinToString("\n") { it.trim() }
        s = BLANK_LINES.replace(s, "\n\n").trim()
        return if (s.length > maxChars) s.take(maxChars) + "…" else s
    }

    /** 提取正文链接（PC 版的【页面链接】就是靠这个让模型继续往下点）。 */
    fun extractLinks(html: String, baseUrl: String, limit: Int = 20): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        val seen = mutableSetOf<String>()
        for (m in LINK.findAll(html)) {
            val href = m.groupValues[1].trim()
            if (href.isEmpty() || href.startsWith("#") || href.startsWith("javascript:")) continue
            val absolute = resolve(baseUrl, href) ?: continue
            if (!seen.add(absolute)) continue
            val text = decodeEntities(TAG.replace(m.groupValues[2], "")).trim()
            if (text.isEmpty()) continue
            out += text.take(80) to absolute
            if (out.size >= limit) break
        }
        return out
    }

    fun decodeEntities(s: String): String = s
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&mdash;", "—")
        .replace("&ndash;", "–")
        .replace("&hellip;", "…")
        .replace("&amp;", "&")

    private fun resolve(base: String, href: String): String? = try {
        java.net.URI(base).resolve(href).toString()
    } catch (_: Exception) {
        null
    }
}