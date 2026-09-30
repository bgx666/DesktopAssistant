package com.xiaozhu.planner.core.image

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import android.util.Base64
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayOutputStream
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

    /** 读一张图 → 缩小摆正 → data URL。读不出来返回 null。 */
    fun toDataUrl(context: Context, uri: Uri): String? = runCatching {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, bounds)
        } ?: return null
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null

        val opts = BitmapFactory.Options().apply {
            inSampleSize = sampleSizeFor(bounds.outWidth, bounds.outHeight)
        }
        val decoded = context.contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, opts)
        } ?: return null

        val oriented = applyExif(context, uri, decoded)
        val scaled = scaleDown(oriented, MAX_SIDE)

        val bytes = ByteArrayOutputStream().use { out ->
            scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
            out.toByteArray()
        }
        if (scaled !== oriented) scaled.recycle()
        if (oriented !== decoded) oriented.recycle()
        decoded.recycle()

        "data:image/jpeg;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
    }.getOrNull()

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
}
