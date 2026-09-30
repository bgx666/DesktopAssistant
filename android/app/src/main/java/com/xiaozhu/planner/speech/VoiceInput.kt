package com.xiaozhu.planner.speech

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * 语音输入：用系统 `SpeechRecognizer`（**不是**云 ASR）。
 *
 * 为什么选它而不是接一个云服务：
 * - 零 key、零成本，多数 ROM 自带离线中文识别；
 * - 权限只有 `RECORD_AUDIO`，不需要把用户说的话再发给第三方；
 * - 少一个供应商、少一处密钥泄漏面。
 * 代价：识别质量随 ROM 差异大，且部分机型离线包要用户自己下。所以它只是
 * **输入的一种方式**——键盘永远在，识别不出来就打字，不影响可用性。
 *
 * 用 `createSpeechRecognizer` 而不是 `createOnDeviceSpeechRecognizer`：
 * 后者在 API 31+ 才有、且很多机器没有离线模型，直接用它会让大部分用户点不动。
 */
class VoiceInput(
    context: Context,
    private val onPartial: (String) -> Unit,
    private val onFinal: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onStateChange: (Boolean) -> Unit,
) {

    private val appContext = context.applicationContext
    private var recognizer: SpeechRecognizer? = null
    private var listening = false

    val available: Boolean
        get() = SpeechRecognizer.isRecognitionAvailable(appContext)

    fun start() {
        if (listening) return
        if (!available) {
            onError("这台设备没有可用的语音识别（多数是没装语音服务）。打字也一样。")
            return
        }

        val engine = recognizer ?: SpeechRecognizer.createSpeechRecognizer(appContext).also {
            it.setRecognitionListener(listener)
            recognizer = it
        }

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            // 说长一点别被截断（默认约 5 秒）
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2000L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 1500L)
        }

        listening = true
        onStateChange(true)
        runCatching { engine.startListening(intent) }
            .onFailure {
                listening = false
                onStateChange(false)
                onError("启动语音识别失败：${it.message}")
            }
    }

    fun stop() {
        if (!listening) return
        listening = false
        onStateChange(false)
        runCatching { recognizer?.stopListening() }
    }

    /** 页面退出时调：SpeechRecognizer 不 destroy 会漏一个连接。 */
    fun destroy() {
        stop()
        runCatching { recognizer?.destroy() }
        recognizer = null
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = Unit
        override fun onBeginningOfSpeech() = Unit
        override fun onRmsChanged(rmsdB: Float) = Unit
        override fun onBufferReceived(buffer: ByteArray?) = Unit
        override fun onEndOfSpeech() {
            listening = false
            onStateChange(false)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            firstResult(partialResults)?.let(onPartial)
        }

        override fun onResults(results: Bundle?) {
            listening = false
            onStateChange(false)
            val text = firstResult(results)
            if (text.isNullOrBlank()) onError("没听清，再说一次？") else onFinal(text)
        }

        override fun onError(error: Int) {
            listening = false
            onStateChange(false)
            onError(describe(error))
        }

        override fun onEvent(eventType: Int, params: Bundle?) = Unit
    }

    private fun firstResult(bundle: Bundle?): String? =
        bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()

    private fun describe(error: Int): String = when (error) {
        SpeechRecognizer.ERROR_NO_MATCH -> "没听清，再说一次？"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "没听到声音。"
        SpeechRecognizer.ERROR_AUDIO -> "录音出错（可能被别的应用占着麦克风）。"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "没有麦克风权限，去设置里给一下。"
        SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT ->
            "识别服务连不上（有些机型的中文识别要联网）。"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "识别忙，等一下再试。"
        else -> "语音识别失败（错误码 $error）。"
    }
}
