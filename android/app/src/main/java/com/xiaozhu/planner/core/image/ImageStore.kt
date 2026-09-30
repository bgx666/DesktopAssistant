package com.xiaozhu.planner.core.image

import android.content.Context
import android.util.Base64
import java.io.File
import java.security.MessageDigest

/**
 * 图片仓库：图片本体存在**文件**里，数据库只留引用。
 *
 * 为什么必须这样：
 * 1. 图片原来是以 base64 存在 `messages.images` 那一列——一张 1568px 的 JPEG
 *    约 300 KB，base64 后约 400 KB，**每发一张图数据库就长 400 KB**，备份文件也跟着膨胀
 * 2. 压缩成记忆节点时，图片本体不能进记忆树（会把节点撑大几百倍），
 *    但"那天有张天空的照片"这件事要留下来——所以留下的是**引用**
 *
 * 引用格式（同时兼容老数据）：
 * - `sha1:<40位十六进制>` —— 新格式，指向 `filesDir/images/<sha1>.jpg`
 * - `data:image/...;base64,...` —— **老格式，永远继续认**
 *
 * 双格式读取是"迁移不可能丢图"的关键：哪怕迁移只做了一半、或者中途失败，
 * 两种格式混着也能正常显示与发送。
 *
 * 内容寻址（按 sha1 命名）顺带解决了去重：同一张图发两次只存一份。
 */
object ImageStore {

    private const val REF_PREFIX = "sha1:"
    private const val DIR_NAME = "images"

    /**
     * 应用上下文（单例注入一次）。
     *
     * 之所以不留成"每个调用点都传 Context"：`MessageMapping` 是纯函数式的对象，
     * 而引用解析要用在读取消息的任何地方（模型上下文、界面、工具检索），
     * 一路传 Context 会把签名污染得到处都是。
     */
    @Volatile
    private var appContext: Context? = null

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    private fun dir(): File {
        val base = appContext?.filesDir ?: error("ImageStore 没有初始化")
        return File(base, DIR_NAME).apply { mkdirs() }
    }

    /** 这个值是不是"文件引用"（新格式）。 */
    fun isRef(value: String): Boolean = value.startsWith(REF_PREFIX)

    /** 这个值是不是"内嵌 base64"（老格式）。 */
    fun isInline(value: String): Boolean = value.startsWith("data:")

    /**
     * 把一张 data URL 存成文件，返回引用（`sha1:<hex>`）。
     *
     * 失败返回 null —— 调用方必须**保留原来的 base64**，绝不能因为存文件失败就丢图。
     */
    fun put(dataUrl: String): String? = runCatching {
        val base64 = dataUrl.substringAfter(',', "")
        if (base64.isEmpty()) return null
        val bytes = Base64.decode(base64, Base64.DEFAULT)
        if (bytes.isEmpty()) return null
        val sha1 = sha1Of(bytes)
        val file = File(dir(), "$sha1.jpg")
        if (!file.exists() || file.length() == 0L) {
            // 先写临时文件再改名：中途失败不会留下"半张图"
            val tmp = File(dir(), "$sha1.tmp")
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(file)) {
                tmp.copyTo(file, overwrite = true)
                tmp.delete()
            }
        }
        if (file.length() == 0L) return null
        REF_PREFIX + sha1
    }.getOrNull()

    /** 引用（或老格式的 base64）→ 可直接发给模型的 data URL。取不到返回 null。 */
    fun dataUrl(value: String): String? = when {
        isInline(value) -> value
        isRef(value) -> fileOf(value.substringAfter(REF_PREFIX))?.let { file ->
            runCatching {
                "data:image/jpeg;base64," + Base64.encodeToString(file.readBytes(), Base64.NO_WRAP)
            }.getOrNull()
        }
        else -> null
    }

    /** 批量解析（读消息时用）。取不到的条目会被跳过。 */
    fun dataUrls(values: List<String>): List<String> = values.mapNotNull { dataUrl(it) }

    fun fileOf(sha1: String): File? {
        val file = File(dir(), "$sha1.jpg")
        return file.takeIf { it.exists() && it.length() > 0 }
    }

    /** 引用还在不在（相册引用失效、文件被清理掉时返回 false）。 */
    fun exists(value: String): Boolean = when {
        isInline(value) -> true
        isRef(value) -> fileOf(value.substringAfter(REF_PREFIX)) != null
        else -> false
    }

    fun delete(value: String): Boolean {
        if (!isRef(value)) return false
        return runCatching { File(dir(), "${value.substringAfter(REF_PREFIX)}.jpg").delete() }.getOrDefault(false)
    }

    /** 占用与张数（设置页与图片页显示用）。 */
    fun stats(): Pair<Int, Long> {
        val files = runCatching { dir().listFiles { f -> f.isFile && f.name.endsWith(".jpg") } }.getOrNull()
            ?: return 0 to 0L
        return files.size to files.sumOf { it.length() }
    }

    /** 全部图片文件（图片页列用），按修改时间倒序。 */
    fun all(): List<File> =
        runCatching {
            dir().listFiles { f -> f.isFile && f.name.endsWith(".jpg") }
                ?.sortedByDescending { it.lastModified() }
        }.getOrNull().orEmpty()

    /** 清理没有 .jpg 收尾的残留（写一半的 .tmp 之类）。 */
    fun cleanupTemp(): Int {
        val temps = runCatching { dir().listFiles { f -> f.name.endsWith(".tmp") } }.getOrNull() ?: return 0
        var removed = 0
        temps.forEach { if (it.delete()) removed++ }
        return removed
    }

    private fun sha1Of(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-1").digest(bytes).joinToString("") { "%02x".format(it) }
}
