package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.SpeechPacing
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.buildJsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `continue_speaking` 的节奏：公式抄 PC 版（2 秒 + 每 10 字 1 秒，上限 10 秒），
 * 两端必须一致——不一致的话同一段话在手机和电脑上听着是两种人。
 */
class ContinueSpeakingToolTest {

    private val slept = mutableListOf<Long>()

    private fun tool(pacing: SpeechPacing) = ContinueSpeakingTool(pacing) { ms -> slept += ms }

    @Test
    fun `空上一段时停 2 秒`() = runBlocking {
        val pacing = SpeechPacing()
        tool(pacing).run(buildJsonObject { })
        assertEquals(listOf(2000L), slept)
    }

    @Test
    fun `每 10 字加 1 秒`() = runBlocking {
        val pacing = SpeechPacing()
        pacing.lastSegmentChars = 35          // 2 + 3.5 = 5.5 秒
        tool(pacing).run(buildJsonObject { })
        assertEquals(5500L, slept.single())
    }

    @Test
    fun `超过 80 字封顶 10 秒`() = runBlocking {
        val pacing = SpeechPacing()
        pacing.lastSegmentChars = 500
        tool(pacing).run(buildJsonObject { })
        assertEquals(10000L, slept.single())
    }

    @Test
    fun `返回一句话让模型知道可以接着说`() = runBlocking {
        val out = tool(SpeechPacing()).run(buildJsonObject { })
        assertTrue(out.contains("接着说下一点"))
    }

    @Test
    fun `工具名与 prompt 里写的一致`() {
        assertEquals("continue_speaking", tool(SpeechPacing()).name)
    }
}
