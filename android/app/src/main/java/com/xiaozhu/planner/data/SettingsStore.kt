package com.xiaozhu.planner.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys

/**
 * 本机设置（LLM 端点/模型/密钥）。密钥用 EncryptedSharedPreferences 加密落盘，
 * 不写明文、不进日志。
 *
 * API 说明：这里用的是 security-crypto **1.0.0（稳定版）** 的接口
 * （`MasterKeys.getOrCreate` + `create(fileName, alias, context, ...)`）。
 * 1.1.0-alpha 那套 `MasterKey.Builder` 的对象式 API 在 1.0.0 里不存在——
 * 换版本时这两处要一起改。
 */
class SettingsStore(context: Context) {

    private val prefs: SharedPreferences = run {
        val masterKeyAlias = MasterKeys.getOrCreate(MasterKeys.AES256_GCM_SPEC)
        EncryptedSharedPreferences.create(
            "planner_secure",
            masterKeyAlias,
            context,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    /** 密钥：写入时统一清洗（去空白/引号/`Bearer ` 前缀/换行），避免把垃圾发进请求头。 */
    var apiKey: String
        get() = prefs.getString(KEY_API, "").orEmpty()
        set(v) = prefs.edit()
            .putString(KEY_API, com.xiaozhu.planner.core.llm.ApiKeyCheck.sanitize(v))
            .apply()

    var baseUrl: String
        get() = prefs.getString(KEY_BASE, DEFAULT_BASE).orEmpty().ifBlank { DEFAULT_BASE }
        set(v) = prefs.edit().putString(KEY_BASE, v.trim().ifBlank { DEFAULT_BASE }).apply()

    var model: String
        get() = prefs.getString(KEY_MODEL, DEFAULT_MODEL).orEmpty().ifBlank { DEFAULT_MODEL }
        set(v) = prefs.edit().putString(KEY_MODEL, v.trim().ifBlank { DEFAULT_MODEL }).apply()

    /**
     * opencode 网关要求的会话头（`x-opencode-session`）。
     *
     * 默认自动生成一个并持久化：那个网关没有这个头会直接 400（MissingSessionID），
     * 而它只需要"一个稳定的标识"，不要求特定值。设置页可以覆盖（万一以后变了）。
     */
    var openCodeSession: String
        get() {
            val saved = prefs.getString(KEY_OC_SESSION, "").orEmpty()
            if (saved.isNotBlank()) return saved
            val fresh = java.util.UUID.randomUUID().toString()
            prefs.edit().putString(KEY_OC_SESSION, fresh).apply()
            return fresh
        }
        set(v) = prefs.edit().putString(KEY_OC_SESSION, v.trim()).apply()

    val configured: Boolean get() = apiKey.isNotBlank()

    /**
     * 一次性迁移：把"沿用的旧默认端点"换成新默认。
     *
     * 为什么需要它：保存过的值优先于默认值，所以只改 `DEFAULT_*` 对**已经装过**的
     * 用户完全无效——设置页里还是那个 opencode 地址，用户以为我没改。
     * 判据刻意收得很紧：**只有保存值正好等于上一版的默认值**才动它
     * （那说明用户从没主动改过）；用户自己填过的一律不碰。
     *
     * @return true 表示这次真的换掉了（界面据此提示一句）
     */
    fun migrateLegacyEndpointIfNeeded(): Boolean {
        if (prefs.getBoolean(KEY_ENDPOINT_MIGRATED, false)) return false
        val savedBase = prefs.getString(KEY_BASE, "").orEmpty()
        val savedModel = prefs.getString(KEY_MODEL, "").orEmpty()
        val isLegacy = com.xiaozhu.planner.core.llm.EndpointUrl.isLegacyOpencodeDefault(savedBase, savedModel)

        prefs.edit().apply {
            if (isLegacy) {
                // 删掉而不是写死新值：这样以后改默认值它还会跟着走
                remove(KEY_BASE)
                remove(KEY_MODEL)
            }
            putBoolean(KEY_ENDPOINT_MIGRATED, true)
        }.apply()
        return isLegacy
    }

    // ── 语音（TTS）────────────────────────────────────────
    // MiMo 是**独立的服务与密钥**（`api.xiaomimimo.com`），不能拿 LLM 的 key 去顶——
    // 两个供应商的 key 互不通用，混用只会得到 401。

    /** 自动朗读小助的回复。默认开：prompt 里已经写明"回复是会被朗读的"。 */
    var ttsEnabled: Boolean
        get() = prefs.getBoolean(KEY_TTS_ENABLED, true)
        set(v) = prefs.edit().putBoolean(KEY_TTS_ENABLED, v).apply()

    /** [TTS_ENGINE_MIMO] 优先、失败退回系统 TTS；[TTS_ENGINE_SYSTEM] 只用系统 TTS。 */
    var ttsEngine: String
        get() = prefs.getString(KEY_TTS_ENGINE, TTS_ENGINE_MIMO).orEmpty().ifBlank { TTS_ENGINE_MIMO }
        set(v) = prefs.edit().putString(KEY_TTS_ENGINE, v.trim().ifBlank { TTS_ENGINE_MIMO }).apply()

    var ttsVoice: String
        get() = prefs.getString(KEY_TTS_VOICE, MIMO_DEFAULT_VOICE).orEmpty().ifBlank { MIMO_DEFAULT_VOICE }
        set(v) = prefs.edit().putString(KEY_TTS_VOICE, v.trim().ifBlank { MIMO_DEFAULT_VOICE }).apply()

    var mimoApiKey: String
        get() = prefs.getString(KEY_MIMO_KEY, "").orEmpty()
        set(v) = prefs.edit().putString(KEY_MIMO_KEY, v.trim()).apply()

    var mimoBaseUrl: String
        get() = prefs.getString(KEY_MIMO_BASE, MIMO_DEFAULT_BASE).orEmpty().ifBlank { MIMO_DEFAULT_BASE }
        set(v) = prefs.edit().putString(KEY_MIMO_BASE, v.trim().ifBlank { MIMO_DEFAULT_BASE }).apply()

    var mimoModel: String
        get() = prefs.getString(KEY_MIMO_MODEL, MIMO_DEFAULT_MODEL).orEmpty().ifBlank { MIMO_DEFAULT_MODEL }
        set(v) = prefs.edit().putString(KEY_MIMO_MODEL, v.trim().ifBlank { MIMO_DEFAULT_MODEL }).apply()

    companion object {
        /**
         * 默认走 **DeepSeek 官方**（与 PC 版同一个端点）。
         *
         * 模型名用 `deepseek-flash`——**实测**（2026-09-30）：
         * - `/models` 里它的 `name` 就是 `DeepSeek-V4.1-Flash`，且 `input_modalities`
         *   含 `image`（能收图）
         * - 官方端点**只认** `deepseek-flash` 与 `deepseek-v4-pro`；
         *   传 `deepseek-v4.1-flash` 会 400（那个名字是 opencode 网关的）
         * - 同样的短请求：官方 ~1.1 秒、opencode 网关 ~3.3 秒（快三倍）
         *
         * 换回 opencode：Base 填 `https://opencode.ai/zen/go/v1`、
         * 模型填 `deepseek-v4.1-flash`（session 头会自动带上）。
         */
        const val DEFAULT_BASE = "https://api.deepseek.com/v1"
        const val DEFAULT_MODEL = "deepseek-flash"
        const val OPENCODE_BASE = "https://opencode.ai/zen/go/v1"

        const val TTS_ENGINE_MIMO = "mimo"
        const val TTS_ENGINE_SYSTEM = "system"
        const val MIMO_DEFAULT_BASE = "https://api.xiaomimimo.com/v1"
        const val MIMO_DEFAULT_MODEL = "mimo-v2.5-tts"
        const val MIMO_DEFAULT_VOICE = "mimo_default"

        private const val KEY_API = "llm_api_key"
        private const val KEY_BASE = "llm_base_url"
        private const val KEY_MODEL = "llm_model"
        private const val KEY_OC_SESSION = "opencode_session"
        private const val KEY_ENDPOINT_MIGRATED = "endpoint_migrated_v1"
        private const val KEY_TTS_ENABLED = "tts_enabled"
        private const val KEY_TTS_ENGINE = "tts_engine"
        private const val KEY_TTS_VOICE = "tts_voice"
        private const val KEY_MIMO_KEY = "mimo_api_key"
        private const val KEY_MIMO_BASE = "mimo_base_url"
        private const val KEY_MIMO_MODEL = "mimo_model"
    }
}