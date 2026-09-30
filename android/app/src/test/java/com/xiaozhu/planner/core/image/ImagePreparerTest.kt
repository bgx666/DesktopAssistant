package com.xiaozhu.planner.core.image

import android.graphics.Bitmap
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 发图前的缩放。
 *
 * **与 PC 版刻意不同**：PC 在电脑上拖图，原图就在同一台机器，直接发原图没有代价；
 * 手机是**上传**，一张 4000×3000 的照片 base64 后 8–12 MB，发一轮要等很久，
 * 而视觉模型对单张图最多按 384 token 计费——发原图纯属浪费。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ImagePreparerTest {

    @Test
    fun `长边超过上限就等比缩小`() {
        val big = Bitmap.createBitmap(4000, 3000, Bitmap.Config.ARGB_8888)
        val small = ImagePreparer.scaleDown(big, 1568)

        assertEquals(1568, maxOf(small.width, small.height))
        // 比例要保持（4000×3000 → 1568×1176）
        assertEquals(1176, small.height)
        big.recycle()
    }

    @Test
    fun `没超过上限就原样返回（不做无谓的重编码）`() {
        val bitmap = Bitmap.createBitmap(800, 600, Bitmap.Config.ARGB_8888)
        assertTrue("同一对象直接返回", bitmap === ImagePreparer.scaleDown(bitmap, 1568))
    }

    @Test
    fun `竖图也按长边缩（不会把图缩成一条）`() {
        val tall = Bitmap.createBitmap(1200, 4800, Bitmap.Config.ARGB_8888)
        val small = ImagePreparer.scaleDown(tall, 1568)
        assertEquals(1568, small.height)
        assertEquals(392, small.width)
        tall.recycle()
    }

    @Test
    fun `降采样倍率是 2 的幂，且降完不超过上限`() {
        // 4000 → 2000 → 1000（再降就小于 1568 了，所以 sample=2）
        assertEquals(2, ImagePreparer.sampleSizeFor(4000, 3000))
        assertEquals(1, ImagePreparer.sampleSizeFor(1568, 1000))
        assertEquals(4, ImagePreparer.sampleSizeFor(8000, 6000))
        // 任何输入下，降采样之后都还大于上限（剩下的交给精确缩放）
        val sample = ImagePreparer.sampleSizeFor(4000, 3000)
        assertTrue(4000 / sample >= 1568)
    }

    @Test
    fun `一次最多四张`() {
        assertEquals(4, ImagePreparer.MAX_IMAGES)
    }

    // ── 解码路径（所有图片的必经之路）────────────────────────
    //
    // 曾经的真实故障：相册选图报"读不出来"。原因是把 provider 的流**直接**交给
    // BitmapFactory——相册/照片选择器给的 URI 背后可能是云相册、HEIC 转码或一次性流，
    // 直接解会失败。改成"先整份复制到本地再解码"之后就通了。这几条用例钉住它。

    private val context: android.content.Context =
        androidx.test.core.app.ApplicationProvider.getApplicationContext()

    /** 造一张真图（真 PNG 字节，不是空文件）。 */
    private fun realPng(width: Int = 120, height: Int = 80): ByteArray {
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(android.graphics.Color.RED)
        return java.io.ByteArrayOutputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
            bitmap.recycle()
            out.toByteArray()
        }
    }

    @Test
    fun `content URI（相册那条路）能读出来`() {
        val uri = android.net.Uri.parse("content://test.media/images/1")
        org.robolectric.Shadows.shadowOf(context.contentResolver)
            .registerInputStream(uri, java.io.ByteArrayInputStream(realPng()))

        val dataUrl = ImagePreparer.toDataUrl(context, uri)

        assertTrue("相册的 content URI 必须能读出来", dataUrl != null)
        assertTrue(dataUrl!!.startsWith("data:image/jpeg;base64,"))
        // 解回去确认内容有效，而不是一段坏 base64
        assertTrue(ImagePreparer.decodeDataUrl(dataUrl) != null)
    }

    @Test
    fun `本地文件 URI（App 内拍照那条路）能读出来`() {
        val file = java.io.File(context.cacheDir, "shot-test.png")
        file.writeBytes(realPng())
        val uri = android.net.Uri.fromFile(file)

        val dataUrl = ImagePreparer.toDataUrl(context, uri)

        assertTrue("拍照存的文件必须能读出来", dataUrl != null)
        assertTrue(ImagePreparer.decodeDataUrl(dataUrl!!) != null)
    }

    @Test
    fun `大图会缩到长边上限以内`() {
        val file = java.io.File(context.cacheDir, "big-test.png")
        file.writeBytes(realPng(2400, 1200))

        val dataUrl = ImagePreparer.toDataUrl(context, android.net.Uri.fromFile(file))!!
        val bitmap = ImagePreparer.decodeDataUrl(dataUrl)!!

        assertTrue("长边应被缩到 1568 以内，实际 ${maxOf(bitmap.width, bitmap.height)}",
            maxOf(bitmap.width, bitmap.height) <= 1568)
        // 比例要保住
        assertEquals(2.0, bitmap.width.toDouble() / bitmap.height, 0.05)
    }

    @Test
    fun `空文件读不出来（相机没写成就是这种情况）`() {
        val file = java.io.File(context.cacheDir, "empty.jpg")
        file.writeBytes(ByteArray(0))

        assertTrue(ImagePreparer.toDataUrl(context, android.net.Uri.fromFile(file)) == null)
        assertTrue(
            "空文件要说清是相机没写进来",
            ImagePreparer.diagnose(context, android.net.Uri.fromFile(file)).contains("空的"),
        )
    }

    // 关于"不是图片的文件"：**不在单测里断言**。
    // Robolectric 的 ShadowBitmapFactory 对任何字节都会合成一张假位图，
    // 所以"解不出来 → 返回 null"这条分支在单测环境里永远走不到（那是真机行为）。
    // 这里只保证它不崩、并给出一句话——真正的判定交给设备。

    @Test
    fun `损坏文件不会崩，且给得出一句诊断`() {
        val file = java.io.File(context.cacheDir, "garbage.bin")
        file.writeBytes(ByteArray(4096) { 0x7F })

        // 不抛异常即可（返回 null 还是假位图取决于运行环境）
        ImagePreparer.toDataUrl(context, android.net.Uri.fromFile(file))
        assertTrue(ImagePreparer.diagnose(context, android.net.Uri.fromFile(file)).isNotBlank())
    }

    @Test
    fun `打不开的 URI 给出可读的诊断而不是崩掉`() {
        val missing = android.net.Uri.fromFile(java.io.File(context.cacheDir, "根本不存在.jpg"))
        assertTrue(ImagePreparer.toDataUrl(context, missing) == null)
        assertTrue(ImagePreparer.diagnose(context, missing).isNotBlank())
    }
}
