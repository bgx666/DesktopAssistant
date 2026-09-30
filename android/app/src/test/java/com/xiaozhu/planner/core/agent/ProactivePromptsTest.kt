package com.xiaozhu.planner.core.agent

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 主动轮次的提示词。这些话是**系统说的、不是用户说的**，措辞错了模型会把
 * 它们当成用户的要求（比如把"你判断该提醒他"读成"用户让我提醒他"），
 * 所以关键要素逐条钉住。
 */
class ProactivePromptsTest {

    @Test
    fun `提醒提示带上时刻与内容，并说明是用户自己排的`() {
        val text = ProactivePrompts.reminder("20:00", listOf("「吃药」（原定 2026-09-30 20:00）"))
        assertTrue(text.startsWith("（系统："))
        assertTrue(text.contains("现在 20:00"))
        assertTrue(text.contains("吃药"))
        assertTrue(text.contains("用户自己排的"))
    }

    @Test
    fun `多项提醒用分号连起来`() {
        val text = ProactivePrompts.reminder("20:00", listOf("A", "B"))
        assertTrue(text.contains("A；B"))
    }

    @Test
    fun `心跳提示说明现在几点、并允许三种选择`() {
        val text = ProactivePrompts.heartbeat("15:30", "看看投递进度")
        assertTrue(text.startsWith("（系统："))
        assertTrue(text.contains("现在 15:30"))
        assertTrue(text.contains("看看投递进度"))
        assertTrue(text.contains("自主学习"))
        assertTrue(text.contains("什么都不做"))
        assertTrue("要提醒它定下一次心跳", text.contains("heartbeat"))
    }

    @Test
    fun `心跳提示带上距用户上次说话多久`() {
        val text = ProactivePrompts.heartbeat("15:30", "", awayFor = "2 小时")
        assertTrue(text.contains("用户上次说话是 2 小时前"))
    }

    @Test
    fun `没给时长与备注时不留下空洞的措辞`() {
        val text = ProactivePrompts.heartbeat("15:30", "")
        assertFalse(text.contains("用户上次说话是 前"))
        assertFalse(text.contains("醒来时要做的：。"))
        assertTrue(text.contains("现在 15:30"))
    }

    @Test
    fun `自主学习那条明确说不要朗读`() {
        val text = ProactivePrompts.heartbeat("15:30", "")
        assertTrue(text.contains("不要朗读"))
    }

    @Test
    fun `补提醒说明刚才不在，别让模型以为是刚排的`() {
        val text = ProactivePrompts.missedReminder("09:00", listOf("「交作业」（原定 2026-09-29 全天）"))
        assertTrue(text.contains("刚才我不在"))
        assertTrue(text.contains("交作业"))
    }

    @Test
    fun `所有主动提示都以系统标记开头`() {
        listOf(
            ProactivePrompts.reminder("20:00", listOf("x")),
            ProactivePrompts.heartbeat("20:00", "n"),
            ProactivePrompts.missedReminder("20:00", listOf("x")),
        ).forEach {
            assertTrue("必须以（系统：开头，prompt 靠这个区分它不是用户消息", it.startsWith("（系统："))
            assertTrue(it.endsWith("）"))
        }
    }
}
