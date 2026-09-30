package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MemorySearchTest {

    @Test
    fun `关键词按空白与逗号切分并清洗`() {
        assertEquals(listOf("华为", "面试"), MemorySearch.terms("华为 面试"))
        assertEquals(listOf("华为", "面试"), MemorySearch.terms("华为,面试"))
        assertEquals(listOf("华为", "面试"), MemorySearch.terms("  华为 ， 面试  "))
        assertEquals(listOf("华为"), MemorySearch.terms("华为 华为"))
    }

    @Test
    fun `LIKE 通配符被剥掉，避免用户输入变成通配符查询`() {
        assertEquals(listOf("100"), MemorySearch.terms("100%"))
        assertEquals(listOf("abc"), MemorySearch.terms("a_bc"))
    }

    @Test
    fun `关键词最多五个`() {
        assertEquals(5, MemorySearch.terms("a b c d e f g").size)
    }

    @Test
    fun `SQL 是每个词都要命中的 AND 查询`() {
        val (sql, args) = MemorySearch.buildSql("assistant", listOf("华为", "面试"), limit = 5)
        assertTrue(sql.contains("WHERE characterId = ?"))
        assertTrue("两个词应该是 AND", sql.contains(" AND "))
        assertEquals("assistant", args[0])
        assertEquals(listOf("%华为%", "%华为%", "%面试%", "%面试%", "5"), args.drop(1))
    }

    @Test
    fun `片段围绕命中词截取并标出省略`() {
        val text = "前面很多字".repeat(20) + "今晚华为那个聊完" + "后面很多字".repeat(20)
        val snip = MemorySearch.snippet(text, listOf("华为"), pad = 6)
        assertTrue(snip.contains("华为"))
        assertTrue("左侧应有省略号", snip.startsWith("…"))
        assertTrue("右侧应有省略号", snip.endsWith("…"))
        assertTrue("片段不该是一整段原文", snip.length < 40)
    }

    @Test
    fun `没命中时退回开头`() {
        val snip = MemorySearch.snippet("只有这些内容", listOf("不存在"), maxLen = 5)
        assertEquals("只有这些内", snip)
    }

    @Test
    fun `可检索文本包含摘要画像与原文`() {
        val text = MemorySearch.searchableText(
            summary = "聊了求职",
            profile = ProfileInfo(preferences = listOf("喜欢晚上学习")),
            messages = listOf(ChatMessage.user("投了中电和宁德时代")),
        )
        assertTrue(text.contains("聊了求职"))
        assertTrue(text.contains("喜欢晚上学习"))
        assertTrue(text.contains("投了中电和宁德时代"))
    }
}