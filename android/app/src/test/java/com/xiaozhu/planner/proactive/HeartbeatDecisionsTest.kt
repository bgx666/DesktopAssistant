package com.xiaozhu.planner.proactive

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 心跳到点走哪条路。三条去向的行为差别很大（界面上说 / 后台跑一轮推通知 / 顺延），
 * 判错的表现是"该说话时没声"或"半夜被吵醒"，所以钉在这里。
 */
class HeartbeatDecisionsTest {

    private val now = 1_000_000L
    private val dndEnd = now + 60_000

    @Test
    fun `界面活着就交给界面说`() {
        assertEquals(
            HeartbeatDecision.ToUi,
            HeartbeatDecisions.decide(inDnd = false, dndEndMillis = 0L, uiAlive = true, nowMillis = now),
        )
    }

    @Test
    fun `界面不在就起后台服务`() {
        assertEquals(
            HeartbeatDecision.ToBackground,
            HeartbeatDecisions.decide(inDnd = false, dndEndMillis = 0L, uiAlive = false, nowMillis = now),
        )
    }

    @Test
    fun `免打扰里顺延到窗口结束，不管界面在不在`() {
        assertEquals(
            HeartbeatDecision.Defer(dndEnd),
            HeartbeatDecisions.decide(inDnd = true, dndEndMillis = dndEnd, uiAlive = true, nowMillis = now),
        )
        assertEquals(
            HeartbeatDecision.Defer(dndEnd),
            HeartbeatDecisions.decide(inDnd = true, dndEndMillis = dndEnd, uiAlive = false, nowMillis = now),
        )
    }

    @Test
    fun `免打扰但算不出结束时刻就跳过`() {
        // 跳过一次心跳没有损失；按 0 去排闹钟会立刻又响一次，变成死循环
        assertEquals(
            HeartbeatDecision.Skip,
            HeartbeatDecisions.decide(inDnd = true, dndEndMillis = 0L, uiAlive = false, nowMillis = now),
        )
        assertEquals(
            HeartbeatDecision.Skip,
            HeartbeatDecisions.decide(inDnd = true, dndEndMillis = now - 1, uiAlive = false, nowMillis = now),
        )
    }
}
