package com.xiaozhu.planner.core.tools

/**
 * Bing 的 `format=rss` 结果解析（纯 Kotlin，可单测）。
 *
 * 选 RSS 而不是抓 HTML：结构稳定、体积小、不需要 JS。
 * 代价是结果偏"网页标题+摘要"，所以还要过滤掉工具导航站/广告大全这类垃圾。
 */
object BingRss {

    data class Hit(val title: String, val url: String, val snippet: String)

    private val ITEM = Regex("(?is)<item\\b.*?</item>")
    private val TITLE = Regex("(?is)<title>(.*?)</title>")
    private val LINK = Regex("(?is)<link>(.*?)</link>")
    private val DESC = Regex("(?is)<description>(.*?)</description>")

    /** PC 版同款黑名单：这些站点的标题党/目录页对"找干货"帮助不大。 */
    val LOW_QUALITY_DOMAINS = setOf(
        "ai-bot.cn", "aigc.cn", "toolify.ai", "top10.com", "futurepedia.io",
        "zhihu.com/topic", "baike.baidu.com", "wikipedia.org",
    )

    fun parse(xml: String, limit: Int): List<Hit> {
        val hits = mutableListOf<Hit>()
        for (item in ITEM.findAll(xml)) {
            val block = item.value
            val title = clean(TITLE.find(block)?.groupValues?.get(1).orEmpty())
            val url = clean(LINK.find(block)?.groupValues?.get(1).orEmpty())
            val snippet = Html.toPlainText(clean(DESC.find(block)?.groupValues?.get(1).orEmpty()), 300)
            if (title.isEmpty() || url.isEmpty()) continue
            if (isLowQuality(title, url)) continue
            hits += Hit(title, url, snippet)
            if (hits.size >= limit) break
        }
        return hits
    }

    fun isLowQuality(title: String, url: String): Boolean {
        val u = url.lowercase()
        if (LOW_QUALITY_DOMAINS.any { u.contains(it) }) return true
        // 纯目录型标题（"XX 导航""XX 大全"）也丢掉
        return Regex("大全|导航站|排行榜前").containsMatchIn(title)
    }

    private fun clean(raw: String): String = Html.decodeEntities(
        raw.replace("<![CDATA[", "").replace("]]>", "")
    ).trim()
}