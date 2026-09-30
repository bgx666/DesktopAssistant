package com.xiaozhu.planner.core.memory

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 记忆压缩参数：默认值、范围、交叉约束都与 PC 版一致
 * （PC 的 settings.html：20–500 / 5–200 / 3–50 / 2–10，默认 60/20/8/4）。
 *
 * 参数不合法时**不拦保存**——PC 就是在示意图上画一行红字，参数照存；
 * 这里保持同样的宽容度，只是把话说清楚。
 */
class CompressionSettingsTest {

    @Test
    fun `默认值与 PC 版一致`() {
        val s = CompressionSettings()
        assertEquals(60, s.trigger)
        assertEquals(20, s.keep)
        assertEquals(8, s.compactThreshold)
        assertEquals(4, s.compactFactor)
        assertNull("默认参数当然是合法的", s.problem())
    }

    @Test
    fun `保留条数不能大于等于触发条数`() {
        val s = CompressionSettings(trigger = 60, keep = 60)
        assertTrue(s.problem()!!.contains("保留条数"))
        // 差一点也不行：相等时永远压不动
        assertTrue(CompressionSettings(trigger = 20, keep = 25).problem() != null)
    }

    @Test
    fun `合并个数不能大于等于合并阈值`() {
        val s = CompressionSettings(compactThreshold = 4, compactFactor = 4)
        assertTrue(s.problem()!!.contains("合并个数"))
        assertNull(CompressionSettings(compactThreshold = 8, compactFactor = 4).problem())
    }

    @Test
    fun `两条约束同时不满足时先报第一条（与 PC 的判定顺序一致）`() {
        val s = CompressionSettings(trigger = 10, keep = 20, compactThreshold = 2, compactFactor = 5)
        assertTrue(s.problem()!!.contains("保留条数"))
    }
}
