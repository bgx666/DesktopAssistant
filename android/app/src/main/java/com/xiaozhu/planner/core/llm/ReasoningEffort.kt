package com.xiaozhu.planner.core.llm

/**
 * 推理档位（`reasoning_effort`）。
 *
 * DeepSeek 的 V4 系列默认就在推理，而且**默认档是 `high`**（`GET /models` 里
 * `effort.default_level = "high"`）。这里显式给 `high`，与 PC 保持一致。
 *
 * **为什么不是 `low`**（曾经是，2026-09-30 改掉）：
 * 一开始只在"一道没营养的短问题"上量过，结论是 low 略快略省；换成**一道真实的方案题**
 * 再量，结论反过来了（deepseek-flash，各 1 次）：
 * - `low`：推理 3005 token、正文 1604 字、20.5 秒
 * - `high`：推理 1645 token、正文 2222 字、15.4 秒
 * - `max`：推理 2615 token、正文 2030 字、18.8 秒
 * 也就是 low **没带来任何好处**，反而推理更多、答得更短、还更慢——
 * 用户体感正是"回答短、不够聪明"。
 * 教训：**别拿简单问题上的测量去决定真实场景的档位**。
 *
 * 另外**不发 `thinking`**：默认本来就开着推理（不发也有上千 reasoning token），
 * 显式打开没收益，实测那次推理反而更多。
 *
 * 还有一条与 PC 不同的地方：PC 的判断条件是 `model_name.startswith("deepseek-v4")`，
 * 而官方现在的主力 id 叫 `deepseek-flash`（正式名 DeepSeek-V4.1-Flash）——
 * **按 PC 那个前缀，用户在官方端点上的模型根本匹配不上**。这里把两种情况都算进来。
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
        return if (isDeepSeekV4) HIGH else null
    }

    /** 设置页的说明文案。 */
    fun describe(effort: String): String = when (effort) {
        LOW -> "low：想得最少（实测并不更快，也不更省）"
        HIGH -> "high：官方默认档，推荐"
        MAX -> "max：想得最久，费钱也最慢"
        else -> effort
    }
}
