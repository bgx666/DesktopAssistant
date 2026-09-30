package com.xiaozhu.planner.core.tools

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BingRssTest {

    private val rss = """
        <?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>小米 MiMo 语音模型发布</title>
            <link>https://example.com/mimo</link>
            <description><![CDATA[官方发布，<b>支持中文</b> TTS。]]></description>
          </item>
          <item>
            <title>AI 工具大全</title>
            <link>https://ai-bot.cn/tools</link>
            <description>导航站</description>
          </item>
          <item>
            <title>另一篇正经文章</title>
            <link>https://blog.example.org/post</link>
            <description>正文摘要 &amp; 更多</description>
          </item>
        </channel></rss>
    """.trimIndent()

    @Test
    fun `解析出标题链接摘要并去掉 CDATA 与标签`() {
        val hits = BingRss.parse(rss, limit = 10)
        assertEquals(2, hits.size) // 黑名单那条被过滤
        assertEquals("小米 MiMo 语音模型发布", hits[0].title)
        assertEquals("https://example.com/mimo", hits[0].url)
        assertTrue(hits[0].snippet.contains("支持中文"))
        assertTrue(hits[1].snippet.contains("正文摘要 & 更多"))
    }

    @Test
    fun `limit 生效`() {
        assertEquals(1, BingRss.parse(rss, limit = 1).size)
    }

    @Test
    fun `低质量站点与目录型标题被识别`() {
        assertTrue(BingRss.isLowQuality("x", "https://ai-bot.cn/a"))
        assertTrue(BingRss.isLowQuality("提示词大全", "https://good.example/x"))
        assertTrue(BingRss.isLowQuality("x", "https://zh.wikipedia.org/wiki/A"))
        assertTrue(!BingRss.isLowQuality("正经文章", "https://blog.example.org/post"))
    }

    @Test
    fun `完全不是 RSS 时返回空表而不是抛异常`() {
        assertEquals(0, BingRss.parse("<html><body>验证码</body></html>", limit = 5).size)
    }
}