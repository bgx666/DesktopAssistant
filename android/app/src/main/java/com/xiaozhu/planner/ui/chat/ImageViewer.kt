package com.xiaozhu.planner.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import com.xiaozhu.planner.core.image.ImagePreparer

/**
 * 全屏看图：点开大图、双指缩放、拖动，**缩小到一定程度自动退回**。
 *
 * 几个手感上的取舍：
 * - 缩放下限设 0.85：手指往中间捏到比这还小，说明用户想关掉，直接退——比找关闭按钮快
 * - 双击在 1× 与 2.5× 之间切换（看细节时最常用的动作）
 * - 单击空白处也退（和大多数看图应用一致）
 * - 关闭按钮仍然留着：不是所有人都知道"捏小可以关"
 */
@Composable
fun ImageViewer(dataUrl: String, onDismiss: () -> Unit) {
    val bitmap = remember(dataUrl) {
        runCatching { ImagePreparer.decodeDataUrl(dataUrl) }.getOrNull()
    }

    var scale by remember { mutableStateOf(1f) }
    var offset by remember { mutableStateOf(Offset.Zero) }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color(0xF2000000))
            // 手势与点击分成两个 modifier：一个管缩放/拖动，一个管单击/双击
            .pointerInput(Unit) {
                detectTransformGestures { _, pan, zoom, _ ->
                    scale = (scale * zoom).coerceIn(MIN_SCALE, MAX_SCALE)
                    // 缩到很小就当作"我要退出"
                    if (scale <= DISMISS_SCALE) {
                        onDismiss()
                    } else {
                        offset += pan
                    }
                }
            }
            .pointerInput(Unit) {
                detectTapGestures(
                    onTap = { onDismiss() },
                    onDoubleTap = {
                        if (scale > 1.05f) {
                            scale = 1f
                            offset = Offset.Zero
                        } else {
                            scale = 2.5f
                        }
                    },
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        if (bitmap != null) {
            androidx.compose.foundation.Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = "放大查看",
                contentScale = ContentScale.Fit,
                modifier = Modifier
                    .fillMaxSize()
                    .graphicsLayer {
                        scaleX = scale
                        scaleY = scale
                        translationX = offset.x
                        translationY = offset.y
                    },
            )
        } else {
            Text("这张图读不出来", color = Color.White)
        }

        // 关闭按钮（右上角）
        Box(
            modifier = Modifier
                .align(Alignment.TopEnd)
                .padding(16.dp)
                .clip(CircleShape)
                .background(Color(0x66000000))
                .clickable(onClick = onDismiss)
                .padding(horizontal = 14.dp, vertical = 8.dp),
        ) {
            Text("关闭", style = MaterialTheme.typography.labelLarge, color = Color.White)
        }

        // 操作提示（左下角，只在原始比例时显示，免得挡着看图）
        if (scale <= 1.05f) {
            Box(
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(16.dp)
                    .clip(CircleShape)
                    .background(Color(0x66000000))
                    .padding(horizontal = 12.dp, vertical = 6.dp),
            ) {
                Text(
                    "双指缩放 · 双击放大 · 捏小退出",
                    style = MaterialTheme.typography.labelMedium,
                    color = Color(0xCCFFFFFF),
                )
            }
        }
    }
}

/** 缩放范围与"捏到多小算退出"。 */
private const val MIN_SCALE = 0.6f
private const val MAX_SCALE = 6f
private const val DISMISS_SCALE = 0.85f
