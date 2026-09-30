package com.xiaozhu.planner.ui.chat

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.provider.MediaStore
import androidx.activity.result.contract.ActivityResultContract

/**
 * 拍照的结果：**原图文件与缩略图都要拿到**。
 *
 * 为什么两个都要：不少国内 ROM 的相机 App（MIUI / HarmonyOS / 各家定制）
 * **忽略 `EXTRA_OUTPUT`**，只把一张缩略图塞在返回 Intent 的 `data` 里。
 * 官方的 `ActivityResultContracts.TakePicture` 把这个 Intent 直接丢掉，
 * 于是我们给的文件始终是 0 字节，界面只能报"这张图读不出来"。
 * 拿到缩略图至少能让这张图发出去，而不是白拍一次。
 */
data class CaptureResult(
    /** 相机返回了 RESULT_OK。 */
    val ok: Boolean,
    /** `data` extra 里的缩略图（Bitmap 形式）。 */
    val thumbnail: Bitmap?,
    /** `data` extra 里的缩略图（Uri 形式，个别 ROM 这么给）。 */
    val thumbnailUri: Uri?,
)

/**
 * 启动系统相机，并且**明确把写入权限授给相机 App**。
 *
 * 相机是另一个进程，要往我们的 FileProvider URI 写数据就必须由我们授权——
 * `TakePicture` 只塞了 `EXTRA_OUTPUT`、没加 `FLAG_GRANT_WRITE_URI_PERMISSION`，
 * 这是"拍完文件是空的"的另一条成因。
 *
 * @param output 我们提供的输出文件 URI（相机应当把原图写到这里）
 */
class CapturePhoto(private val output: Uri) : ActivityResultContract<Unit, CaptureResult>() {

    override fun createIntent(context: Context, input: Unit): Intent =
        Intent(MediaStore.ACTION_IMAGE_CAPTURE)
            .putExtra(MediaStore.EXTRA_OUTPUT, output)
            .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)

    override fun parseResult(resultCode: Int, intent: Intent?): CaptureResult {
        val ok = resultCode == android.app.Activity.RESULT_OK
        val raw = intent?.extras?.get("data")
        return CaptureResult(
            ok = ok,
            thumbnail = raw as? Bitmap,
            thumbnailUri = raw as? Uri,
        )
    }
}
