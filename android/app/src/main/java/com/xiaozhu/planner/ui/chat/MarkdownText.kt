package com.xiaozhu.planner.ui.chat

import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import com.xiaozhu.planner.core.text.Markdown
import com.xiaozhu.planner.ui.theme.PlannerColors

/**
 * 把 [Markdown] 解析出来的段落转成 Compose 的富文本。
 *
 * 与 PC 版 `md.js` + `style.css` 的 `.bubble strong/code/.md-code` 对应：
 * 加粗半粗、行内代码等宽 + 浅底、代码块等宽 + 浅底 + 稍小字号。
 * PC 版是把代码块渲染成独立的 `<pre>`；在手机气泡里保持行内（但保留换行）更省空间，
 * 也不会因为横向滚动破坏布局。
 */
@Composable
fun markdownText(
    text: String,
    baseColor: Color = PlannerColors.Text,
    boldColor: Color? = null,
): AnnotatedString {
    val spans = Markdown.parse(text)
    return buildAnnotatedString {
        spans.forEach { span ->
            when (span.style) {
                Markdown.Style.Plain -> withStyle(SpanStyle(color = baseColor)) { append(span.text) }
                Markdown.Style.Bold -> withStyle(
                    SpanStyle(color = boldColor ?: baseColor, fontWeight = FontWeight.SemiBold)
                ) { append(span.text) }

                Markdown.Style.Code -> withStyle(
                    SpanStyle(
                        color = boldColor ?: baseColor,
                        fontFamily = FontFamily.Monospace,
                        background = PlannerColors.SurfaceSoft,
                    )
                ) { append(span.text) }

                Markdown.Style.CodeBlock -> withStyle(
                    SpanStyle(
                        color = boldColor ?: baseColor,
                        fontFamily = FontFamily.Monospace,
                        background = PlannerColors.SurfaceSoft,
                    )
                ) { append(span.text) }
            }
        }
    }
}
