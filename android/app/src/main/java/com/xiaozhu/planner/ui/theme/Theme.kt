package com.xiaozhu.planner.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 配色与形状**取自 PC 版前端**（`frontend/renderer/style.css` 的 CSS 变量），
 * 这样两端看起来是同一个东西，而不是"另一个 App"：
 *
 * ```
 * --primary: #5b7cfa    --primary-2: #8aa8ff   --accent: #3ddc97
 * --text:    #2a3040    --text-soft: #8b94ab   --danger: #ff6b81  --ok: #2fbf8f
 * ```
 *
 * 小助的头像是 PC 版的"渐变球"：`radial-gradient(circle at 32% 28%, #8ff0e8,
 * #39c5bb 60%, #1fa89f)` 中间一个白点——手机上也画成同一个。
 */
object PlannerColors {
    val Primary = Color(0xFF5B7CFA)
    val PrimaryLight = Color(0xFF8AA8FF)
    val Accent = Color(0xFF3DDC97)
    val Text = Color(0xFF2A3040)
    val TextSoft = Color(0xFF8B94AB)
    val Danger = Color(0xFFFF6B81)
    val Ok = Color(0xFF2FBF8F)

    /** 小助头像的三段渐变（与 PC 版同一组色）。 */
    val AvatarGradient = Brush.radialGradient(
        colors = listOf(Color(0xFF8FF0E8), Color(0xFF39C5BB), Color(0xFF1FA89F)),
        radius = 34f,
    )

    /** 用户气泡：PC 版是 135° 的蓝色渐变。 */
    val UserBubble = Brush.linearGradient(listOf(Color(0xFF6F8DF9), Primary))

    /** 页面底色：PC 版是浅蓝灰，不是纯白。 */
    val Background = Color(0xFFF4F6FB)
    val Surface = Color(0xFFFFFFFF)
    val SurfaceSoft = Color(0xFFEDF1F9)

    /** 记忆卡片：虚线感靠浅蓝底 + 蓝边（Compose 画虚线要自绘，这里用色调表达）。 */
    val MemoryCard = Color(0xFFEEF2FE)
    val MemoryBorder = Color(0x595B7CFA)
}

private val scheme = lightColorScheme(
    primary = PlannerColors.Primary,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE3E9FF),
    onPrimaryContainer = PlannerColors.Text,
    secondary = PlannerColors.Accent,
    onSecondary = Color.White,
    tertiary = Color(0xFF39C5BB),
    background = PlannerColors.Background,
    onBackground = PlannerColors.Text,
    surface = PlannerColors.Surface,
    onSurface = PlannerColors.Text,
    surfaceVariant = PlannerColors.SurfaceSoft,
    onSurfaceVariant = PlannerColors.TextSoft,
    error = PlannerColors.Danger,
    outline = Color(0xFFD8DEEA),
)

private val shapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(14.dp),
    large = RoundedCornerShape(18.dp),
    extraLarge = RoundedCornerShape(24.dp),
)

/** 中文排版：行高放宽一点，字号别太小（手机上要读得清）。 */
private val typography = Typography(
    titleLarge = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold, lineHeight = 28.sp),
    titleMedium = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold, lineHeight = 24.sp),
    titleSmall = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.SemiBold, lineHeight = 20.sp),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontSize = 12.sp, lineHeight = 18.sp),
    labelLarge = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.Medium),
    labelMedium = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.Medium),
)

@Composable
fun PlannerTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = scheme,
        shapes = shapes,
        typography = typography,
        content = content,
    )
}
