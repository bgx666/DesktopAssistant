package com.xiaozhu.planner.core.llm

/**
 * 推理档位（`reasoning_effort`）。
 *
 * DeepSeek 的 V4 系列默认就在推理，而且**默认档是 `high`**（`GET /models` 里
 * `effort.default_level = "high"`）。PC 版显式把它降到 `low`（`llm.py:64-65`），
 * 为的是快一点、省一点、别那么啰嗦——这里对齐同一意图。
 *
 * 实测（2026-09-30，DeepSeek 官方，同一个问题各 3 次）：
 * - 不发字段：推理 172/274/167 token，延迟 2.19/2.08/1.97 秒
 * - `reasoning_effort=low`：推理 154/169/202，延迟 1.85/2.05/1.90 秒
 * - `thinking:{type:enabled}` + low：推理 215、延迟 2.32 秒 → **更差**
 *
 * 所以这里**只发 `reasoning_effort`**，不发 `thinking`：
 * 默认本来就开着推理（不发字段也有上百 reasoning token），
 * 再显式打开没有收益，实测那次反而推理得更多。
 *
 * 注意 PC 的判断条件是 `model_name.startswith("deepseek-v4")`，
 * 而官方现在的主力 id 叫 `deepseek-flash`（正式名 DeepSeek-V4.1-Flash）——
 * **按 PC 那个前缀，用户在官方端点上的模型根本匹配不上**，档位设置等于没生效。
 * 这里把两种情况都算进来。
 */
object ReasoningEffort {

    const val LOW = "low"
    const val HIGH = "high"
    const val MAX = "max"

    /** 官方 `/models` 里 deepseek-flash 声明支持的档位。 */
    val SUPPORTED = listOf(LOW, HIGH, MAX)

    /** 这个模型该用什么档位；不属于 DeepSeek V4 家族就返回 null（不发这个字段）。 */
    fun forModel(model: String): String? {
        val id = model.trim().lowercase()
        if (id.isEmpty()) return null
        val isDeepSeekV4 = id.startsWith("deepseek-v4") ||
            id == "deepseek-flash" ||
            id.startsWith("deepseek-flash-")
        return if (isDeepSeekV4) LOW else null
    }

    /** 设置页的说明文案。 */
    fun describe(effort: String): String = when (effort) {
        LOW -> "low：最快最省，适合日常陪聊与提醒"
        HIGH -> "high：官方默认，想得更久一些"
        MAX -> "max：想得最久，费钱也最慢"
        else -> effort
    }
}
