package com.xiaozhu.planner.core.llm

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * 「思考」的开关与强度 —— 直接对应官方 API 的两个字段。
 *
 * 官方 `thinking.type` 接受三个值（实测 2026-09-30，DeepSeek 官方）：
 * - `adaptive`：**让模型自己决定**要不要想（不传字段时的默认行为最接近这个）
 * - `enabled`：总是先想
 * - `disabled`：**真的不想** —— 实测 reasoning token = 0，同一道题快约 30%
 *
 * 强度用 `reasoning_effort`：`low` / `high`（官方默认）/ `max`。
 * 实测要点：
 * - **`disabled` 时 `reasoning_effort` 会被忽略**（`disabled + effort=high` 仍是 0 推理），
 *   所以界面上关掉思考就该把强度藏起来，免得给一个不生效的控件
 * - 简单题上三档差别很小（20–30 reasoning token），**难题上差别才明显**
 * - `low` 曾经是我们发的档位，实测在真实方案题上反而更差（详见 ReasoningEffort 的注释）
 *
 * 非 DeepSeek V4 家族的模型**两个字段都不发**：那是官方专有参数，
 * 发给别的服务商只会 400。
 */
object ThinkingControl {

    const val ADAPTIVE = "adaptive"
    const val ENABLED = "enabled"
    const val DISABLED = "disabled"

    /** 界面上的三选一（顺序就是显示顺序）。 */
    val MODES = listOf(ADAPTIVE, ENABLED, DISABLED)

    val EFFORTS = listOf(ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX)

    fun modeLabel(mode: String): String = when (mode) {
        ADAPTIVE -> "自适应"
        ENABLED -> "总是思考"
        DISABLED -> "关闭"
        else -> mode
    }

    fun modeHint(mode: String): String = when (mode) {
        ADAPTIVE -> "让模型自己决定要不要想：简单问题直接答，复杂的会先想"
        ENABLED -> "每次回答前都想一遍（更稳，但简单问题也会多花时间）"
        DISABLED -> "完全不思考：最快，但复杂问题会明显变差"
        else -> ""
    }

    fun effortLabel(effort: String): String = when (effort) {
        ReasoningEffort.LOW -> "少想一点"
        ReasoningEffort.HIGH -> "默认"
        ReasoningEffort.MAX -> "多想一点"
        else -> effort
    }

    /** 这个模型支持不支持这套参数（只有 DeepSeek V4 家族支持）。 */
    fun supports(model: String): Boolean = ReasoningEffort.forModel(model) != null

    /**
     * 要合并进请求体的字段。不支持就直接返回 null（一个都不发）。
     *
     * 注意 `disabled` 时**不发** `reasoning_effort`：官方会忽略它，
     * 发了只会让人误以为强度还在起作用。
     */
    fun payload(model: String, mode: String, effort: String): JsonObject? {
        if (!supports(model)) return null
        val safeMode = if (mode in MODES) mode else ADAPTIVE
        return buildJsonObject {
            put("thinking", buildJsonObject { put("type", safeMode) })
            if (safeMode != DISABLED) {
                put("reasoning_effort", if (effort in EFFORTS) effort else ReasoningEffort.HIGH)
            }
        }
    }
}
