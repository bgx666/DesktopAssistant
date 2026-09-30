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

    var apiKey: String
        get() = prefs.getString(KEY_API, "").orEmpty()
        set(v) = prefs.edit().putString(KEY_API, v.trim()).apply()

    var baseUrl: String
        get() = prefs.getString(KEY_BASE, DEFAULT_BASE).orEmpty().ifBlank { DEFAULT_BASE }
        set(v) = prefs.edit().putString(KEY_BASE, v.trim().ifBlank { DEFAULT_BASE }).apply()

    var model: String
        get() = prefs.getString(KEY_MODEL, DEFAULT_MODEL).orEmpty().ifBlank { DEFAULT_MODEL }
        set(v) = prefs.edit().putString(KEY_MODEL, v.trim().ifBlank { DEFAULT_MODEL }).apply()

    val configured: Boolean get() = apiKey.isNotBlank()

    companion object {
        const val DEFAULT_BASE = "https://api.deepseek.com/v1"
        const val DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"
        private const val KEY_API = "llm_api_key"
        private const val KEY_BASE = "llm_base_url"
        private const val KEY_MODEL = "llm_model"
    }
}