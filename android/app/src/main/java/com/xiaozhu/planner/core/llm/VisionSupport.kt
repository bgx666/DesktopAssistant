package com.xiaozhu.planner.core.llm

/**
 * 这个模型能不能"看图"。
 *
 * **为什么不能只按模型名猜**：PC 版是名字里含 vision/-vl/-omni/multimodal/gpt-4o/gpt-4.1
 * 才算视觉模型。而 DeepSeek 官方现在的主力模型叫 `deepseek-flash`——
 * 名字里一个相关词都没有，但它**确实支持图片**（`GET /models` 里
 * `input_modalities: ["text","image"]`，正式名 DeepSeek-V4.1-Flash）。
 * 只按名字猜会把能看图的模型误判成不能。
 *
 * 所以这里两重判断：**已知能看图的 id 白名单** + 名字启发式（兜住别的服务商）。
 * 判错的代价不对称：判成"不能"只是少个功能并提示用户；
 * 判成"能"却发了图，端点会直接 400。
 */
object VisionSupport {

    /** 实测确认支持图片输入的模型（2026-09-30 查 `GET /models`）。 */
    private val KNOWN_VISION_IDS = setOf(
        "deepseek-flash",             // 正式名 DeepSeek-V4.1-Flash，input_modalities 含 image
        "deepseek-v4-flash-vision-exp",
    )

    /** 名字里带这些词的，按视觉模型处理（别的服务商的常见命名）。 */
    private val NAME_HINTS = listOf(
        "vision", "-vl", "vl-", "omni", "multimodal", "gpt-4o", "gpt-4.1", "claude-3", "claude-4",
        "gemini", "qwen-vl", "glm-4v", "internvl", "minicpm-v",
    )

    fun isVisionCapable(model: String): Boolean {
        val id = model.trim().lowercase()
        if (id.isEmpty()) return false
        if (id in KNOWN_VISION_IDS) return true
        return NAME_HINTS.any { id.contains(it) }
    }

    /** 发图被拦下时给用户的话（告诉他换成什么）。 */
    fun unsupportedHint(model: String): String =
        "当前模型「$model」看不了图片。换成 deepseek-flash（正式名 DeepSeek-V4.1-Flash，" +
            "官方实测支持图片输入）就能直接发图。"
}
