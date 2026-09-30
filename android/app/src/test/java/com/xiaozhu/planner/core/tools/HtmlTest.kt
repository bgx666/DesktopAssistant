package com.xiaozhu.planner.core.tools

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class HtmlTest {

    @Test
    fun `去掉脚本样式与注释，保留可读文本`() {
        val html = """
            <html><head><style>body{color:red}</style>
            <script>var x = "<b>不是内容</b>";</script></head>
            <body><!-- 注释 --><h1>标题</h1><p>第一段</p><p>第二段</p></body></html>
        """.trimIndent()
        val text = Html.toPlainText(html, 1000)
        assertTrue(text.contains("标题"))
        assertTrue(text.contains("第一段"))
        assertFalse("脚本内容不该出现", text.contains("不是内容"))
        assertFalse("样式不该出现", text.contains("color:red"))
        assertFalse("注释不该出现", text.contains("注释"))
    }

    @Test
    fun `实体被还原且空白被收敛`() {
        val text = Html.toPlainText("<p>a &amp; b&nbsp;&nbsp;c</p>\n\n\n<p>d &lt;e&gt;</p>", 1000)
        assertTrue(text.contains("a & b"))
        assertTrue(text.contains("d <e>"))
        assertFalse("不该有连续三个换行", text.contains("\n\n\n"))
    }

    @Test
    fun `超长正文被截断并加省略号`() {
        val text = Html.toPlainText("<p>" + "字".repeat(500) + "</p>", 100)
        assertEquals(101, text.length) // 100 字 + 省略号
        assertTrue(text.endsWith("…"))
    }

    @Test
    fun `链接提取会补全相对地址并去重`() {
        val html = """
            <a href="/a">甲</a>
            <a href="https://x.com/b">乙</a>
            <a href="/a">甲的重复</a>
            <a href="#top">锚点</a>
            <a href="javascript:void(0)">脚本</a>
        """.trimIndent()
        val links = Html.extractLinks(html, "https://x.com/start", limit = 10)
        assertEquals(listOf("https://x.com/a", "https://x.com/b"), links.map { it.second })
        assertEquals("甲", links[0].first)
    }
}