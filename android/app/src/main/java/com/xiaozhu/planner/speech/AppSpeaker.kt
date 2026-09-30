package com.xiaozhu.planner.speech

import android.content.Context
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.speech.tts.TextToSpeech
import com.xiaozhu.planner.core.speech.MiMoTts
import com.xiaozhu.planner.core.speech.Speaker
import com.xiaozhu.planner.core.speech.SpeechText
import com.xiaozhu.planner.data.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.util.Locale

/**
 * 手机上怎么把话说出来。
 *
 * 两条路，**默认优先 MiMo**（与 PC 版同一把嗓子），失败或没配置时退回系统 TTS：
 *
 * - MiMo：`/chat/completions` 拿整段 wav 字节 → 写缓存文件 → MediaPlayer 播。
 *   为什么落文件：MiMo 返回的是完整 wav 而不是流，`setDataSource(File)` 是
 *   MediaPlayer 最稳的输入方式，也不需要额外权限。
 * - 系统 TTS：零配置、离线可用，音色随 ROM；**只做兜底**，不做主力。
 *
 * 一个刻意的行为：**用户一发消息就停朗读**（ChatViewModel 调 [stop]）——
 * 手机上听着长回复想插话时，最烦的就是"它还在念"。
 */
class AppSpeaker(
    context: Context,
    private val settings: SettingsStore,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Main),
) : Speaker {

    private val appContext = context.applicationContext
    private var player: MediaPlayer? = null
    private var job: Job? = null

    private var tts: TextToSpeech? = null
    private var ttsReady = false
    /** TTS 引擎初始化期间来的朗读请求（第一次点朗读不能是哑的）。 */
    private var pendingSystemText: String? = null

    override val available: Boolean
        get() = settings.ttsEnabled

    override fun speak(text: String) {
        if (!settings.ttsEnabled) return
        if (!SpeechText.worthSpeaking(text)) return
        stop()

        val spoken = SpeechText.clean(text)
        job = scope.launch {
            val bytes = if (settings.ttsEngine == SettingsStore.TTS_ENGINE_MIMO) {
                withContext(Dispatchers.IO) { synthesizeWithMiMo(spoken) }
            } else {
                null
            }
            if (bytes != null && playWav(bytes)) return@launch
            // 没配 key / 请求失败 / 播放失败 → 系统 TTS 兜底（有声音总比没有好）
            speakWithSystem(spoken)
        }
    }

    override fun stop() {
        job?.cancel()
        job = null
        runCatching {
            player?.let { if (it.isPlaying) it.stop() }
            player?.release()
        }
        player = null
        runCatching { tts?.stop() }
        pendingSystemText = null
    }

    override fun isSpeaking(): Boolean = runCatching { player?.isPlaying == true }.getOrDefault(false)

    /** 释放资源（Activity 销毁时调；TextToSpeech 不 shutdown 会漏一个 binder 线程）。 */
    fun shutdown() {
        stop()
        runCatching { tts?.shutdown() }
        tts = null
        ttsReady = false
    }

    // ── MiMo ────────────────────────────────────────────────

    private fun synthesizeWithMiMo(spoken: String): ByteArray? {
        val key = settings.mimoApiKey
        if (key.isBlank()) return null
        return runCatching {
            MiMoTts(
                apiKey = key,
                baseUrl = settings.mimoBaseUrl,
                model = settings.mimoModel,
                voice = settings.ttsVoice,
            ).synthesize(spoken)
        }.getOrNull()
    }

    /** 返回是否真的开始播了。 */
    private fun playWav(bytes: ByteArray): Boolean = runCatching {
        val file = File(appContext.cacheDir, TTS_CACHE_FILE)
        file.writeBytes(bytes)

        player = MediaPlayer().apply {
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            setDataSource(file.absolutePath)
            setOnCompletionListener { mp ->
                runCatching { mp.release() }
                if (player === mp) player = null
            }
            prepare()
            start()
        }
        true
    }.getOrDefault(false)

    // ── 系统 TTS 兜底 ────────────────────────────────────────

    private fun speakWithSystem(spoken: String) {
        if (spoken.isBlank()) return
        val engine = ensureTts()
        if (ttsReady) {
            utter(engine, spoken)
        } else {
            pendingSystemText = spoken      // 初始化完成后自动补念
        }
    }

    private fun ensureTts(): TextToSpeech {
        tts?.let { return it }
        lateinit var created: TextToSpeech
        created = TextToSpeech(appContext) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (!ttsReady) return@TextToSpeech
            runCatching { created.language = Locale.SIMPLIFIED_CHINESE }
            pendingSystemText?.let { text ->
                pendingSystemText = null
                utter(created, text)
            }
        }
        tts = created
        return created
    }

    private fun utter(engine: TextToSpeech, spoken: String) {
        runCatching {
            if (engine.isLanguageAvailable(Locale.SIMPLIFIED_CHINESE) >= TextToSpeech.LANG_AVAILABLE) {
                engine.language = Locale.SIMPLIFIED_CHINESE
            }
            engine.speak(spoken, TextToSpeech.QUEUE_FLUSH, null, UTTERANCE_ID)
        }
    }

    private companion object {
        const val UTTERANCE_ID = "planner_reply"
        const val TTS_CACHE_FILE = "tts_reply.wav"
    }
}
