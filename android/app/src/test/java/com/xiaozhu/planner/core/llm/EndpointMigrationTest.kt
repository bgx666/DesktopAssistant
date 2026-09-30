package com.xiaozhu.planner.core.llm

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 端点的一次性迁移判据。
 *
 * 背景：保存过的值优先于默认值，所以只改默认值对**已经装过**的用户完全无效——
 * 设置页里还是旧地址，用户会以为"你根本没改"。
 *
 * 但迁移不能滥：只有**保存值正好等于上一版默认值**时才动它
 * （那说明用户从没主动改过，只是沿用了 app 填的）。用户自己填的一律不碰。
 */
class EndpointMigrationTest {

    private val legacyBase = EndpointUrl.LEGACY_OPENCODE_BASE      // https://opencode.ai/zen/go/v1
    private val legacyModel = EndpointUrl.LEGACY_OPENCODE_MODEL    // deepseek-v4.1-flash

    @Test
    fun `旧默认值 → 该迁移`() {
        assertTrue(EndpointUrl.isLegacyOpencodeDefault(legacyBase, legacyModel))
        // 带 /chat/completions、带尾斜杠、带空格的写法都算同一个（归一化后比较）
        assertTrue(EndpointUrl.isLegacyOpencodeDefault("$legacyBase/chat/completions", legacyModel))
        assertTrue(EndpointUrl.isLegacyOpencodeDefault("  $legacyBase/  ", legacyModel))
    }

    @Test
    fun `用户自己改过的值 → 不迁移`() {
        // 地址是 opencode，但模型名是他自己挑的
        assertFalse(EndpointUrl.isLegacyOpencodeDefault(legacyBase, "deepseek-v4-pro"))
        // 地址是他自己填的别的服务商
        assertFalse(EndpointUrl.isLegacyOpencodeDefault("https://api.deepseek.com/v1", legacyModel))
        assertFalse(EndpointUrl.isLegacyOpencodeDefault("https://my-proxy.example/v1", legacyModel))
        // 空的（从没填过）不算"沿用旧默认"，本来就会走新默认
        assertFalse(EndpointUrl.isLegacyOpencodeDefault("", ""))
    }

    @Test
    fun `opencode 的别的模型名也不算旧默认`() {
        assertFalse(EndpointUrl.isLegacyOpencodeDefault(legacyBase, "deepseek-flash"))
        assertFalse(EndpointUrl.isLegacyOpencodeDefault(legacyBase, "kimi-k3"))
    }
}
