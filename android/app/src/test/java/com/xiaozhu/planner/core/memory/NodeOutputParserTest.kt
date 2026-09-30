package com.xiaozhu.planner.core.memory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class NodeOutputParserTest {

    private val goodJson = """
        {"summary":"聊了求职进展","profile":{"preferences":["喜欢晚上学习"],"goals":["三个月内学完 Python"]},
         "future_notes":["原定 15:00，后来改成 16:00"]}
    """.trimIndent()

    @Test
    fun `标准 JSON 直接解析`() {
        val out = NodeOutputParser.parse(goodJson)
        assertNotNull(out)
        assertEquals("聊了求职进展", out!!.summary)
        assertEquals(listOf("喜欢晚上学习"), out.profile.preferences)
        assertEquals(listOf("三个月内学完 Python"), out.profile.goals)
        assertEquals(listOf("原定 15:00，后来改成 16:00"), out.futureNotes)
    }

    @Test
    fun `带 markdown 围栏也能解析`() {
        val out = NodeOutputParser.parse("```json\n$goodJson\n```")
        assertNotNull(out)
        assertEquals("聊了求职进展", out!!.summary)
    }

    @Test
    fun `JSON 前后有废话时抠出对象`() {
        val out = NodeOutputParser.parse("好的，这是压缩结果：\n$goodJson\n希望有帮助！")
        assertNotNull(out)
        assertEquals("聊了求职进展", out!!.summary)
    }

    @Test
    fun `字段缺失时用默认值而不是崩掉`() {
        val out = NodeOutputParser.parse("""{"summary":"只给了摘要"}""")
        assertNotNull(out)
        assertEquals("只给了摘要", out!!.summary)
        assertTrue(out.profile.isEmpty)
        assertTrue(out.futureNotes.isEmpty())
    }

    @Test
    fun `完全不是 JSON 时退化成整段摘要`() {
        val out = NodeOutputParser.parse("这段对话讲了用户找工作的事，投了中电和宁德时代。")
        assertNotNull(out)
        assertTrue(out!!.summary.contains("投了中电和宁德时代"))
    }

    @Test
    fun `空输入返回 null`() {
        assertNull(NodeOutputParser.parse("   "))
        assertNull(NodeOutputParser.parse("```\n\n```"))
    }

    @Test
    fun `嵌套 JSON 也能抠完整`() {
        val nested = """{"summary":"s","profile":{"preferences":["a{b}c"],"personality":[],"habits":[],"goals":[]}}"""
        assertEquals(nested, NodeOutputParser.extractJsonObject("前缀 $nested 后缀"))
    }

    @Test
    fun `字符串里的花括号不会打乱配对`() {
        val weird = """{"summary":"他说了 {这个} 和 } 那个","profile":{"preferences":[],"personality":[],"habits":[],"goals":[]}}"""
        val extracted = NodeOutputParser.extractJsonObject(weird)!!
        assertEquals(weird, extracted)
        assertNotNull(NodeOutputParser.parse(weird))
    }
}