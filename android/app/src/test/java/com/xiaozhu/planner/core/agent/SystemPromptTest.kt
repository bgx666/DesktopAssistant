package com.xiaozhu.planner.core.agent

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

class SystemPromptTest {

    private val now = ZonedDateTime.of(2026, 9, 30, 8, 5, 0, 0, ZoneId.of("Asia/Shanghai"))

    @Test
    fun `注入当前时间且保留模板正文`() {
        val out = SystemPrompt.build("正文A\n正文B", now)
        assertTrue(out.startsWith("正文A"))
        assertTrue(out.contains("正文B"))
        assertTrue(out.contains("2026-09-30 08:05"))
        assertTrue(out.contains("UTC+8"))
    }

    @Test
    fun `没有议程时不塞空段落`() {
        val out = SystemPrompt.build("T", now, agenda = null, availableTools = emptyList())
        assertFalse(out.contains("当前议程"))
    }

    @Test
    fun `议程原样注入`() {
        val out = SystemPrompt.build("T", now, agenda = "[当前议程] 明天 15:00 开会")
        assertTrue(out.contains("[当前议程] 明天 15:00 开会"))
    }

    @Test
    fun `可用工具清单会写进提示词，并明确未接入的别假装`() {
        val out = SystemPrompt.build("T", now, availableTools = listOf("heartbeat", "web_search"))
        assertTrue(out.contains("heartbeat"))
        assertTrue(out.contains("web_search"))
        assertTrue("必须明说没接入的做不到", out.contains("还没接入"))
        assertTrue(out.contains("不要假装"))
    }
}