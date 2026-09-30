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

    private fun assistant(id: Long, text: String) = UiMessage(
        id = id,
        kind = UiMessageKind.Assistant,
        text = text,
    )

    private fun user(id: Long, text: String) = UiMessage(
        id = id,
        kind = UiMessageKind.User,
        text = text,
    )

    @Test
    fun `回读结果里已经有了就不重复追加（这次的 bug）`() {
        val built = listOf(user(1, "在吗"), assistant(2, "在。说吧，什么事？"))
        val tail = assistant(99, "在。说吧，什么事？")

        val merged = HistoryMerge.merge(built, tail)

        assertEquals("不该出现两条一样的回答", 2, merged.size)
        assertEquals(1, merged.count { it.kind == UiMessageKind.Assistant })
    }

    @Test
    fun `落盘失败（回读结果里没有）时仍然接上`() {
        val built = listOf(user(1, "在吗"))
        val tail = assistant(99, "在。说吧，什么事？")

        val merged = HistoryMerge.merge(built, tail)

        assertEquals("落盘失败也不能让用户觉得它没回", 2, merged.size)
        assertTrue(merged.last().text.contains("在。"))
    }

    @Test
    fun `没有尾巴时原样返回`() {
        val built = listOf(user(1, "在吗"))
        assertEquals(built, HistoryMerge.merge(built, null))
    }

    @Test
    fun `空白差异不算重复判定失败`() {
        val built = listOf(assistant(2, "  好  "))
        val tail = assistant(99, "好")
        assertEquals("两端空白不该让它变成两条", 1, HistoryMerge.merge(built, tail).size)
    }

    @Test
    fun `同样的文本出现在更早的位置也算已存在`() {
        // 用户可能反复问同一句、它反复答同一句；只要回读结果里有就不该再追加
        val built = listOf(
            user(1, "在吗"), assistant(2, "在。"), user(3, "在吗"), assistant(4, "在。"),
        )
        val tail = assistant(99, "在。")
        assertEquals(4, HistoryMerge.merge(built, tail).size)
    }

    @Test
    fun `空文本的尾巴不会被当成重复而丢掉`() {
        val built = listOf(user(1, "在吗"))
        val tail = assistant(99, "")
        // 空文本无从比对，保守起见接上（界面上显示"（这轮没有输出）"之类）
        assertEquals(2, HistoryMerge.merge(built, tail).size)
    }
}
