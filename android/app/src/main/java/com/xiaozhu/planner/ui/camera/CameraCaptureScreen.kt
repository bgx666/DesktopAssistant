package com.xiaozhu.planner.ui.camera

import android.content.Context
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.xiaozhu.planner.ui.theme.PlannerColors
import java.io.File

/**
 * App 内拍照（CameraX）。
 *
 * **为什么不用系统相机的 Intent**：那要依赖第三方相机 App 的行为，各家 ROM 不一致——
 * 有的忽略 `EXTRA_OUTPUT`（文件是空的）、有的拒绝写我们给的 FileProvider URI、
 * 有的明明写了文件却返回"取消"。用户那边反复失败就是这个原因。
 * 自己拍就把相机 App 从链路里彻底去掉：预览、快门、存文件全在我们进程里。
 *
 * 拍完交给 [onCaptured] 一个**本地文件 URI**，由调用方走和相册同一条解码路径
 * （先落本地再解码，见 ImagePreparer）。
 */
@Composable
fun CameraCaptureScreen(
    onCaptured: (android.net.Uri) -> Unit,
    onCancel: () -> Unit,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current

    var imageCapture by remember { mutableStateOf<ImageCapture?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    val previewView = remember {
        PreviewView(context).apply {
            scaleType = PreviewView.ScaleType.FILL_CENTER
            implementationMode = PreviewView.ImplementationMode.COMPATIBLE
        }
    }

    // 快门：定义在 UI 之前（局部函数必须先声明后用），闭包每次重组都会拿到最新的 imageCapture
    val takePhoto: () -> Unit = {
        val capture = imageCapture
        if (capture != null && !busy) {
            busy = true
            val dir = File(context.cacheDir, "photos").apply { mkdirs() }
            val file = File(dir, "shot-${System.currentTimeMillis()}.jpg")
            capture.takePicture(
                ImageCapture.OutputFileOptions.Builder(file).build(),
                ContextCompat.getMainExecutor(context),
                object : ImageCapture.OnImageSavedCallback {
                    override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                        busy = false
                        if (file.length() > 0) {
                            onCaptured(android.net.Uri.fromFile(file))
                        } else {
                            error = "照片是空的（写文件失败）。"
                        }
                    }

                    override fun onError(exception: ImageCaptureException) {
                        busy = false
                        error = "拍照失败：${exception.message}"
                    }
                },
            )
        }
    }

    DisposableEffect(lifecycleOwner) {
        val future = ProcessCameraProvider.getInstance(context)
        val executor = ContextCompat.getMainExecutor(context)
        future.addListener({
            runCatching {
                val provider = future.get()
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                val capture = ImageCapture.Builder()
                    // 拍文档/白板，清晰度优先
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                    .build()
                val selector = if (provider.hasCamera(CameraSelector.DEFAULT_BACK_CAMERA)) {
                    CameraSelector.DEFAULT_BACK_CAMERA
                } else {
                    CameraSelector.DEFAULT_FRONT_CAMERA
                }
                provider.unbindAll()
                provider.bindToLifecycle(lifecycleOwner, selector, preview, capture)
                imageCapture = capture
            }.onFailure {
                // 绑不上（没有摄像头、被别的 App 占着、模拟器无相机）→ 说清楚
                error = "打不开摄像头：${it.message ?: it.javaClass.simpleName}"
            }
        }, executor)
        onDispose {
            runCatching { ProcessCameraProvider.getInstance(context).get().unbindAll() }
        }
    }

    Box(modifier = Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(factory = { previewView }, modifier = Modifier.fillMaxSize())

        Row(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            horizontalArrangement = Arrangement.Start,
        ) {
            Text(
                "取消",
                style = MaterialTheme.typography.titleMedium,
                color = Color.White,
                modifier = Modifier
                    .clip(CircleShape)
                    .background(Color(0x66000000))
                    .clickable(onClick = onCancel)
                    .padding(horizontal = 14.dp, vertical = 8.dp),
            )
        }

        Column(
            modifier = Modifier.align(Alignment.BottomCenter).padding(bottom = 36.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            error?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = Color(0xFFFFB4B4),
                    modifier = Modifier.padding(bottom = 12.dp, start = 24.dp, end = 24.dp),
                )
            }
            Box(
                modifier = Modifier
                    .size(74.dp)
                    .clip(CircleShape)
                    .background(if (busy) Color(0x66FFFFFF) else Color.White)
                    .clickable(enabled = !busy && imageCapture != null, onClick = takePhoto),
                contentAlignment = Alignment.Center,
            ) {
                Box(
                    modifier = Modifier
                        .size(58.dp)
                        .clip(CircleShape)
                        .background(if (busy) Color(0xFF9AA3B2) else PlannerColors.Primary),
                )
            }
            Text(
                if (busy) "正在保存…" else "点一下拍照",
                style = MaterialTheme.typography.labelMedium,
                color = Color.White,
                modifier = Modifier.padding(top = 10.dp),
            )
        }
    }
}
