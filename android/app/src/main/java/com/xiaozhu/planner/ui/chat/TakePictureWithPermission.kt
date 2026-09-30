package com.xiaozhu.planner.ui.chat

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.activity.result.contract.ActivityResultContracts

/**
 * 拍照，并且**明确把写入权限授给相机 App**。
 *
 * 这是"拍完读不出来"的真正原因：`ActivityResultContracts.TakePicture` 只往
 * `ACTION_IMAGE_CAPTURE` 里塞了 `EXTRA_OUTPUT`，**没有加**
 * `FLAG_GRANT_WRITE_URI_PERMISSION`。相机 App 拿不到写权限 → 写不进我们的
 * FileProvider URI → 文件是空的（0 字节）→ 解码失败，界面报"这张图读不出来"。
 *
 * 相机 App 是**另一个进程**，它要往我们的 URI 写数据就必须由我们授权，
 * 这一步没有任何隐式行为可以依赖（各厂商相机实现不一样，有的会自己忽略错误、
 * 有的直接返回 RESULT_CANCELED）。
 *
 * 顺带把读权限也授出去：部分相机在拍完做缩略图回显时会读一次。
 */
class TakePictureWithPermission : ActivityResultContracts.TakePicture() {
    override fun createIntent(context: Context, input: Uri): Intent =
        super.createIntent(context, input).apply {
            addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
}
