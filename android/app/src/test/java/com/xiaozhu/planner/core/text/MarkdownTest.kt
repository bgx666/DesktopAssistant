package com.xiaozhu.planner.core.text

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Markdown 解析：与 PC 版 `frontend/renderer/md.js` 逐条对齐。
 *
 * 这里最容易出的错是"**丢字**"——PC 版渲染成 HTML 时，正则没匹配上的内容
 * 会原样留在 HTML 里；而我这边是解析成段，漏一段就是消息内容少了一截。
 * 所以每个用例都额外断言"拼回来等于原文（去掉标记）"。
 */
class MarkdownTest {

    private fun joined(text: String): String = Markdown.parse(text).joinToString("") { it.text }

    @Test
    fun `加粗`() {
        val spans = Markdown.parse("这是**重点**内容")
        assertEquals(
            listOf(
                Markdown.MarkdownSpan("这是", Markdown.Style.Plain),
                Markdown.MarkdownSpan("重点", Markdown.Style.Bold),
                Markdown.MarkdownSpan("内容", Markdown.Style.Plain),
            ),
            spans,
        )
        assertEquals("这是重点内容", joined("这是**重点**内容"))
    }

    @Test
    fun `行内代码`() {
        val spans = Markdown.parse("跑 `npm start` 就行")
        assertEquals(
            listOf(
                Markdown.MarkdownSpan("跑 ", Markdown.Style.Plain),
                Markdown.MarkdownSpan("npm start", Markdown.Style.Code),
                Markdown.MarkdownSpan(" 就行", Markdown.Style.Plain),
            ),
            spans,
        )
    }

    @Test
    fun `代码块`() {
        val spans = Markdown.parse("看这段：\n```\nval x = 1\n```\n完")
        val block = spans.single { it.style == Markdown.Style.CodeBlock }
        assertEquals("val x = 1", block.text)
        assertEquals("看这段：\nval x = 1\n完", joined("看这段：\n```\nval x = 1\n```\n完"))
    }

    @Test
    fun `代码块里的加粗不解析（与 PC 版同序：先抠块）`() {
        val spans = Markdown.parse("```\n**不是加粗**\n```")
        assertEquals(1, spans.size)
        assertEquals(Markdown.Style.CodeBlock, spans.single().style)
        assertEquals("**不是加粗**", spans.single().text)
    }

    @Test
    fun `行内代码里的星号不解析`() {
        val spans = Markdown.parse("`a**b**c`")
        assertEquals(1, spans.size)
        assertEquals(Markdown.Style.Code, spans.single().style)
        assertEquals("a**b**c", spans.single().text)
    }

    @Test
    fun `未闭合的标记原样保留，不吞字`() {
        assertEquals("这是**没闭合", joined("这是**没闭合"))
        assertEquals("半个 `code", joined("半个 `code"))
        assertEquals("```\n没闭合的块", joined("```\n没闭合的块"))
    }

    @Test
    fun `空输入返回空`() {
        assertTrue(Markdown.parse(null).isEmpty())
        assertTrue(Markdown.parse("").isEmpty())
    }

    @Test
    fun `多行文本与换行都保住`() {
        val text = "第一行\n第二行\n\n第四行"
        assertEquals(text, joined(text))
    }

    @Test
    fun `中文标点与 emoji 不受影响`() {
        val text = "好，**行**。（笑）😄"
        assertEquals("好，行。（笑）😄", joined(text))
    }

    @Test
    fun `多个标记混排`() {
        val spans = Markdown.parse("**A** 和 `B` 还有 **C**")
        assertEquals(
            listOf(Markdown.Style.Bold, Markdown.Style.Plain, Markdown.Style.Code,
                Markdown.Style.Plain, Markdown.Style.Bold),
            spans.map { it.style },
        )
        assertEquals("A 和 B 还有 C", joined("**A** 和 `B` 还有 **C**"))
    }

    @Test
    fun `纯文本版用于朗读与通知`() {
        assertEquals("这是重点内容", Markdown.toPlainText("这是**重点**内容"))
        assertEquals("跑 npm start 就行", Markdown.toPlainText("跑 `npm start` 就行"))
    }

    @Test
    fun `工具中文名（PC 版 TOOL_DISPLAY 的子集）`() {
        assertEquals("设置定时唤醒", Markdown.toolDisplayName("heartbeat"))
        assertEquals("查看日程", Markdown.toolDisplayName("list_schedule"))
        // 没翻的保持原名：模型与日志里都是英文名，乱翻反而对不上
        assertEquals("some_new_tool", Markdown.toolDisplayName("some_new_tool"))
    }
}
