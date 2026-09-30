package com.xiaozhu.planner.core.memory

import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.data.db.NodeEntity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NodeRendererTest {

    private fun node(
        id: String = "node0_001",
        level: Int = 0,
        summary: String = "聊了求职安排",
        profile: String? = """{"preferences":["喜欢晚上学习"],"goals":["三个月内学完 Python"]}""",
    ) = NodeEntity(
        characterId = "assistant",
        id = id,
        level = level,
        summary = summary,
        roundStart = 0,
        roundEnd = 39,
        timeStart = "2026-09-13 20:00:00",
        timeEnd = "2026-09-13 20:30:00",
        profile = profile,
    )

    @Test
    fun `时间范围同日只显示一次日期`() {
        assertEquals(
            "2026-09-13 20:00 ~ 20:30",
            NodeRenderer.fmtTimeRange("2026-09-13 20:00:00", "2026-09-13 20:30:00"),
        )
    }

    @Test
    fun `时间范围跨日显示完整日期`() {
        assertEquals(
            "2026-09-13 20:00 ~ 2026-09-14 09:05",
            NodeRenderer.fmtTimeRange("2026-09-13 20:00:00", "2026-09-14 09:05:00"),
        )
    }

    @Test
    fun `进上下文的节点消息带摘要时间画像与后续说明`() {
        val text = NodeRenderer.renderForContext(
            node(),
            LeafDetails(futureNotes = listOf("原定 15:00，后来改成 16:00")),
        )
        assertTrue(text.contains("[node0_001] 第0-39条"))
        assertTrue(text.contains("[摘要] 聊了求职安排"))
        assertTrue(text.contains("[时间] 2026-09-13 20:00 ~ 20:30"))
        assertTrue(text.contains("[喜好] 喜欢晚上学习"))
        assertTrue(text.contains("[目标] 三个月内学完 Python"))
        assertTrue(text.contains("[后续说明] 原定 15:00，后来改成 16:00"))
    }

    @Test
    fun `没画像时不输出空标签`() {
        val text = NodeRenderer.renderForContext(node(profile = null), LeafDetails())
        assertFalse(text.contains("[喜好]"))
        assertFalse(text.contains("[后续说明]"))
    }

    @Test
    fun `叶子展开给原文`() {
        val info = NodeInfo(
            nodeId = "node0_001",
            level = 0,
            summary = "聊了求职",
            timeRange = "2026-09-13 20:00:00" to "2026-09-13 20:30:00",
            profile = null,
            details = listOf(ChatMessage.user("投了中电"), ChatMessage.assistant("行，记上了。")),
        )
        val text = NodeRenderer.renderForTool(info)
        assertTrue(text.contains("原文（2 条）"))
        assertTrue(text.contains("用户：投了中电"))
        assertTrue(text.contains("小助：行，记上了。"))
    }

    @Test
    fun `父节点展开给子节点清单`() {
        val info = NodeInfo(
            nodeId = "node1_001",
            level = 1,
            summary = "两个月的求职线",
            timeRange = null,
            profile = null,
            children = listOf(
                NodeInfo("node0_001", 0, "聊了中电", null, null),
                NodeInfo("node0_002", 0, "聊了宁德时代", null, null),
            ),
        )
        val text = NodeRenderer.renderForTool(info)
        assertTrue(text.contains("子节点（2 个"))
        assertTrue(text.contains("node0_001：聊了中电"))
        assertFalse("父节点不该有原文段", text.contains("原文（"))
    }
}