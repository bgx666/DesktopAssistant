package com.xiaozhu.planner.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回读历史与"刚生成的那条"的合并。
 *
 * 真实故障：用户只问了一句「在吗」，界面上出现**两个一模一样的回答**。
 * 原因是一轮结束后无条件把内存里的回复接在回读结果后面，而它已经落盘了。
 */
class HistoryMergeTest {

    private fun assistant(id: Long, text: String, rawIndex: Int? = null) = UiMessage(
        id = id,
        kind = UiMessageKind.Assistant,
        text = text,
        rawIndex = rawIndex,
    )

    private fun user(id: Long, text: String, rawIndex: Int? = null) = UiMessage(
        id = id,
        kind = UiMessageKind.User,
        text = text,
        rawIndex = rawIndex,
    )

    @Test
    fun `落盘成功就不接内存那份（这次的多段拼接 bug）`() {
        // 一轮里模型调了两次：内存里那条是两个输出拼在一起的，
        // 跟库里任何单条都不相等——按文本比对会误判成"库里没有"，于是又接一遍
        val built = listOf(
            user(1, "论文，我会努力的"),
            assistant(2, "行，那就不喊口号了。今天只推一件事……"),
            assistant(3, "傍晚我再来问一句。"),
        )
        val tail = assistant(99, "行，那就不喊口号了。今天只推一件事……傍晚我再来问一句。")

        // 这一轮有新原文落库（40 → 42）→ 落盘成功 → 丢掉内存那份
        val merged = HistoryMerge.merge(built, tail, rawBefore = 40, rawAfter = 42)

        assertEquals("拼接版不该再出现一次", 3, merged.size)
    }

    @Test
    fun `没有新原文落库时才接上（落盘真失败）`() {
        val built = listOf(user(1, "在吗"))
        val tail = assistant(99, "在。说吧，什么事？")

        val merged = HistoryMerge.merge(built, tail, rawBefore = 40, rawAfter = 40)

        assertEquals("落盘失败也不能让用户觉得它没回", 2, merged.size)
    }

    @Test
    fun `不传 rawBefore 时保守保留（兼容旧调用）`() {
        val built = listOf(user(1, "在吗"))
        val tail = assistant(99, "在。")
        assertEquals(2, HistoryMerge.merge(built, tail, rawBefore = null, rawAfter = 40).size)
    }

    @Test
    fun `没有尾巴时原样返回`() {
        val built = listOf(user(1, "在吗"))
        assertEquals(built, HistoryMerge.merge(built, null, rawBefore = 1, rawAfter = 2))
    }

    @Test
    fun `空文本的尾巴在落盘失败时也会接上`() {
        val built = listOf(user(1, "在吗"))
        val tail = assistant(99, "")
        assertEquals(2, HistoryMerge.merge(built, tail, rawBefore = 40, rawAfter = 40).size)
    }

    // ── 新消息的原文序号（撤销靠它定位）──────────────────────

    @Test
    fun `已有的原文条数就是新消息的序号`() {
        // 真实数据里从库回读的消息都带 rawIndex（0、1、2…）
        val messages = listOf(
            user(1, "第一句", rawIndex = 0),
            assistant(2, "回第一句", rawIndex = 1),
            user(3, "第二句", rawIndex = 2),
        )
        assertEquals(3, rawIndexOfNewMessage(messages, historyLoaded = true))
    }

    @Test
    fun `记忆卡片不算原文`() {
        val messages = listOf(
            UiMessage(id = 1, kind = UiMessageKind.Memory, text = "很久以前"),
            user(2, "刚说的", rawIndex = 0),
        )
        // 记忆卡片没有 rawIndex，所以只数到 1
        assertEquals(1, rawIndexOfNewMessage(messages, historyLoaded = true))
    }

    @Test
    fun `历史还没载入时算不出来，返回 null（宁可暂时不给按钮）`() {
        assertEquals(null, rawIndexOfNewMessage(emptyList(), historyLoaded = false))
        assertEquals(
            null,
            rawIndexOfNewMessage(listOf(user(1, "x")), historyLoaded = false),
        )
    }

    @Test
    fun `空列表且历史已载入时是第 0 条`() {
        assertEquals(0, rawIndexOfNewMessage(emptyList(), historyLoaded = true))
    }
}
