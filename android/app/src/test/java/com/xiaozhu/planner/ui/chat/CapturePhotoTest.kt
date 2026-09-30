package com.xiaozhu.planner.ui.chat

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 拍照结果解析。
 *
 * 重点在**缩略图那条路**：不少国内 ROM 的相机忽略 `EXTRA_OUTPUT`，只回一张
 * 缩略图。官方 `ActivityResultContracts.TakePicture` 会把那个 Intent 丢掉，
 * 于是文件是 0 字节、界面只能报"读不出来"——白拍一次。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CapturePhotoTest {

    private val output: Uri = Uri.parse("content://com.xiaozhu.planner.fileprovider/photos/shot.jpg")
    private val contract = CapturePhoto(output)

    @Test
    fun `启动相机的 Intent 里带上了输出 URI 与写权限`() {
        val intent = contract.createIntent(
            org.robolectric.RuntimeEnvironment.getApplication(),
            Unit,
        )
        assertEquals(android.provider.MediaStore.ACTION_IMAGE_CAPTURE, intent.action)
        assertEquals(output, intent.getParcelableExtra(android.provider.MediaStore.EXTRA_OUTPUT))
        // 没有这个标志，相机（另一个进程）写不进我们的 URI —— 这是"文件是空的"的成因之一
        assertTrue(
            "必须授写权限",
            intent.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION != 0,
        )
        assertTrue(intent.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
    }

    @Test
    fun `相机只回缩略图时也能拿到它`() {
        val thumb = Bitmap.createBitmap(120, 90, Bitmap.Config.ARGB_8888)
        val intent = Intent().putExtra("data", thumb)

        val result = contract.parseResult(Activity.RESULT_OK, intent)

        assertTrue(result.ok)
        assertEquals(120, result.thumbnail?.width)
        assertNull(result.thumbnailUri)
    }

    @Test
    fun `个别 ROM 用 Uri 形式回缩略图`() {
        val uri = Uri.parse("content://media/external/images/media/42")
        val result = contract.parseResult(Activity.RESULT_OK, Intent().putExtra("data", uri))

        assertTrue(result.ok)
        assertEquals(uri, result.thumbnailUri)
        assertNull(result.thumbnail)
    }

    @Test
    fun `什么都没回时两个都是 null（交给上层给出可读的错误）`() {
        val result = contract.parseResult(Activity.RESULT_OK, Intent())
        assertTrue(result.ok)
        assertNull(result.thumbnail)
        assertNull(result.thumbnailUri)
    }

    @Test
    fun `用户取消时 ok 为 false`() {
        assertFalse(contract.parseResult(Activity.RESULT_CANCELED, null).ok)
    }

    @Test
    fun `位图可以编码成 data URL（缩略图那条路要用）`() {
        val bitmap = Bitmap.createBitmap(64, 48, Bitmap.Config.ARGB_8888)
        val dataUrl = com.xiaozhu.planner.core.image.ImagePreparer.fromBitmap(bitmap)

        assertTrue(dataUrl!!.startsWith("data:image/jpeg;base64,"))
        // 编回去还能解出来，说明内容有效
        val back = com.xiaozhu.planner.core.image.ImagePreparer.decodeDataUrl(dataUrl)
        assertEquals(64, back!!.width)
    }
}
