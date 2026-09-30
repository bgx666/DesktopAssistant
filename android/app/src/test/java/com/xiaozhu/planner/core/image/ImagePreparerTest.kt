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
}
