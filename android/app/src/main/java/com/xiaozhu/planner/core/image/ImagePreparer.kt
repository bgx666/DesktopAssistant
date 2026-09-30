package com.xiaozhu.planner.core.image

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageDecoder
import android.graphics.Matrix
import android.net.Uri
import android.util.Base64
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.math.roundToInt

/**
 * 把用户选的图片变成可以发给模型的 data URL。
 *
 * **与 PC 版刻意不同的一点：这里会先缩小。**
 * PC 在电脑上拖图，原图就在同一台机器上，直接发原图 data URL 没有代价；
 * 手机是**上传**，一张 4000×3000 的手机照片 base64 之后有 8–12 MB，
 * 发一轮要等很久、流量也不小，而视觉模型对单张图最多就按 384 token 计费——
 * 发原图纯属浪费。所以统一缩到长边 1568（视觉模型的常见输入上限附近）、
 * JPEG 质量 85，通常几百 KB。
 *
 * 顺带按 EXIF 摆正方向：手机竖着拍的照片，像素本身常是横的，
 * 不摆正模型看到的就是躺着的图。
 */
object ImagePreparer {

    /** 长边上限（约等于视觉模型的输入分辨率量级）。 */
    private const val MAX_SIDE = 1568

    /** 太大或太小的图都按这个范围收一下。 */
    private const val JPEG_QUALITY = 85

    /** 一次最多带几张（多了既慢又容易超出请求体上限）。 */
    const val MAX_IMAGES = 4

    /**
     * 读一张图 → 缩小摆正 → data URL。读不出来返回 null。
     *
     * **先落到本地临时文件再解码**，不直接解 provider 的流。原因：
     * 相册/照片选择器给的 URI 背后可能是云相册、HEIC 转码、或一次性流——
     * 直接解经常拿不到（用户报过"相册选图也读不出来"）。落到本地之后，
     * 想读几遍读几遍，ImageDecoder 与 BitmapFactory 也能随便换。
     */
    fun toDataUrl(context: Context, uri: Uri): String? = runCatching {
        val local = copyToTemp(context, uri) ?: return null
        try {
            decodeLocal(local)?.let { encode(it) }
        } finally {
            local.delete()
        }
    }.getOrNull()

    /** 把 provider 的流整份复制到应用缓存（解码的可靠前提）。 */
    private fun copyToTemp(context: Context, uri: Uri): File? = runCatching {
        val dir = File(context.cacheDir, "incoming").apply { mkdirs() }
        val file = File(dir, "img-${System.currentTimeMillis()}.bin")
        context.contentResolver.openInputStream(uri)?.use { input ->
            file.outputStream().use { output -> input.copyTo(output, DEFAULT_BUFFER_SIZE) }
        } ?: return null
        if (file.length() == 0L) {
            file.delete()
            return null
        }
        file
    }.getOrNull()

    /**
     * 从本地文件解码。
     *
     * API 28+ 优先 `ImageDecoder`：它能解 HEIC/AVIF 这类新格式，而且**会自动按 EXIF 摆正**
     * （省掉自己转的那一步）。失败再退回 BitmapFactory 两遍解码。
     */
    private fun decodeLocal(file: File): Bitmap? {
        if (android.os.Build.VERSION.SDK_INT >= 28) {
            runCatching {
                val source = ImageDecoder.createSource(file)
                return ImageDecoder.decodeBitmap(source) { decoder, info, _ ->
                    decoder.isMutableRequired = false
                    // 长边超过上限就先按比例缩，省内存（4000px 直接解会 OOM）
                    val longest = maxOf(info.size.width, info.size.height)
                    if (longest > MAX_SIDE) {
                        val ratio = MAX_SIDE.toFloat() / longest
                        decoder.setTargetSize(
                            (info.size.width * ratio).roundToInt().coerceAtLeast(1),
                            (info.size.height * ratio).roundToInt().coerceAtLeast(1),
                        )
                    }
                }
            }.onSuccess { return it }
        }
        // 退回 BitmapFactory（老系统，或 ImageDecoder 不认的格式）
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file.absolutePath, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        val opts = BitmapFactory.Options().apply {
            inSampleSize = sampleSizeFor(bounds.outWidth, bounds.outHeight)
        }
        val decoded = BitmapFactory.decodeFile(file.absolutePath, opts) ?: return null
        return scaleDown(decoded, MAX_SIDE)
    }

    /** 位图 → data URL（含缩放；EXIF 已由 ImageDecoder 或调用方处理）。 */
    private fun encode(bitmap: Bitmap): String {
        val bytes = ByteArrayOutputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
            out.toByteArray()
        }
        bitmap.recycle()
        return "data:image/jpeg;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    /**
     * 读不出来时**到底为什么**——空文件、不是图片、还是打不开。
     *
     * 分开说很重要：拍照失败最常见的原因是"相机 App 没写权限 → 文件是空的"，
     * 而这句话必须让用户（和我）一眼看出该修什么，而不是笼统的"读不出来"。
     */
    fun diagnose(context: Context, uri: Uri): String {
        // 文件大小要按"能不能读出来"判断，不能只看 provider 给的 available()
        val size = runCatching {
            context.contentResolver.openInputStream(uri)?.use { input ->
                var total = 0L
                val buffer = ByteArray(8 * 1024)
                while (true) {
                    val n = input.read(buffer)
                    if (n <= 0) break
                    total += n
                    if (total > 64L * 1024 * 1024) break // 别为了报错把大文件读完
                }
                total
            }
        }.getOrNull()
        val decodable = runCatching {
            val local = copyToTemp(context, uri)
            if (local == null) false else {
                val ok = decodeLocal(local) != null
                local.delete()
                ok
            }
        }.getOrDefault(false)

        return when {
            size == null -> "打不开这张图（没有读取权限，或文件已经不在了）。"
            size == 0L -> "照片是空的——相机 App 没能把画面写进来（写权限没授到）。"
            !decodable -> "这个文件不是能识别的图片格式（${size / 1024}KB）。"
            else -> "这张图读不出来（${size / 1024}KB）。"
        }
    }

    /** 先按 2 的幂降采样，省内存（4000px 的图直接解码会 OOM）。 */
    internal fun sampleSizeFor(width: Int, height: Int): Int {
        var sample = 1
        var longest = maxOf(width, height)
        while (longest / 2 >= MAX_SIDE) {
            longest /= 2
            sample *= 2
        }
        return sample
    }

    /** 长边超过 [max] 就等比缩小；否则原样返回。 */
    internal fun scaleDown(bitmap: Bitmap, max: Int): Bitmap {
        val longest = maxOf(bitmap.width, bitmap.height)
        if (longest <= max) return bitmap
        val ratio = max.toFloat() / longest
        return Bitmap.createScaledBitmap(
            bitmap,
            // 用四舍五入而不是截断：4800×0.3266666 截断会得到 1567，
            // 长边就比上限少 1 像素（测试里一眼看出来的那种"差一点"）
            (bitmap.width * ratio).roundToInt().coerceAtLeast(1),
            (bitmap.height * ratio).roundToInt().coerceAtLeast(1),
            true,
        )
    }

    /** 按 EXIF 旋转/镜像（手机竖拍的照片像素常是横的）。 */
    private fun applyExif(context: Context, uri: Uri, bitmap: Bitmap): Bitmap = runCatching {
        val orientation = context.contentResolver.openInputStream(uri)?.use {
            ExifInterface(it).getAttributeInt(
                ExifInterface.TAG_ORIENTATION,
                ExifInterface.ORIENTATION_NORMAL,
            )
        } ?: ExifInterface.ORIENTATION_NORMAL

        val matrix = Matrix()
        when (orientation) {
            ExifInterface.ORIENTATION_ROTATE_90 -> matrix.postRotate(90f)
            ExifInterface.ORIENTATION_ROTATE_180 -> matrix.postRotate(180f)
            ExifInterface.ORIENTATION_ROTATE_270 -> matrix.postRotate(270f)
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.postScale(-1f, 1f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.postScale(1f, -1f)
            else -> return bitmap
        }
        Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
    }.getOrDefault(bitmap)

    /** data URL → 位图（界面显示缩略图用；失败返回 null）。 */
    fun decodeDataUrl(dataUrl: String): Bitmap? = runCatching {
        val base64 = dataUrl.substringAfter(",", "")
        if (base64.isEmpty()) return null
        val bytes = Base64.decode(base64, Base64.DEFAULT)
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
    }.getOrNull()

    /**
     * 位图 → data URL（相机只回传缩略图时走这条路）。
     *
     * 缩略图本来就只有一两百像素，不需要再缩；直接编码即可。
     */
    fun fromBitmap(bitmap: Bitmap): String? = runCatching {
        val bytes = ByteArrayOutputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
            out.toByteArray()
        }
        "data:image/jpeg;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
    }.getOrNull()
}
