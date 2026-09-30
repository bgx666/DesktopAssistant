package com.xiaozhu.planner

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.xiaozhu.planner.proactive.AppPresence
import com.xiaozhu.planner.reminder.ReminderNotifications
import com.xiaozhu.planner.ui.calendar.CalendarScreen
import com.xiaozhu.planner.ui.calendar.CalendarViewModel
import com.xiaozhu.planner.ui.chat.ChatScreen
import com.xiaozhu.planner.ui.chat.ChatViewModel
import com.xiaozhu.planner.ui.memory.MemoryScreen
import com.xiaozhu.planner.ui.settings.SettingsScreen
import com.xiaozhu.planner.ui.theme.PlannerTheme

/**
 * 壳：四个页面（聊天 / 日历 / 记忆 / 设置）。没有上导航库——几个页面一个状态切换就够，
 * 少一个依赖少一处出错。
 *
 * 通知权限在这里要一次：日历的"到点提醒"完全依赖它，用户拒绝的话提醒会静默消失。
 *
 * 另外它还是**导入旧数据的入口**：在文件管理器或聊天软件里点开导出文件时，
 * 系统会把 URI 送到这里，直接跳到设置页并开始读那个文件。
 */
class MainActivity : ComponentActivity() {

    private var incomingFile by mutableStateOf<Uri?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ReminderNotifications.ensureChannel(this)
        incomingFile = importUriFrom(intent)
        setContent { PlannerApp(incomingFile, onFileConsumed = { incomingFile = null }) }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        importUriFrom(intent)?.let { incomingFile = it }
    }

    override fun onStart() {
        super.onStart()
        AppPresence.foreground = true
    }

    override fun onStop() {
        AppPresence.foreground = false
        super.onStop()
    }

    /** VIEW（点开文件）/ SEND（分享给"小助"）两种都收。 */
    private fun importUriFrom(intent: Intent?): Uri? = when (intent?.action) {
        Intent.ACTION_VIEW -> intent.data
        Intent.ACTION_SEND -> @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_STREAM)
        else -> null
    }
}

private enum class Screen { Chat, Calendar, Memory, Settings }

@Composable
fun PlannerApp(
    incomingFile: Uri? = null,
    onFileConsumed: () -> Unit = {},
) {
    PlannerTheme {
        Surface(modifier = Modifier.fillMaxSize()) {
            val vm: ChatViewModel = viewModel()
            val calendarVm: CalendarViewModel = viewModel()
            var screen by remember { mutableStateOf(Screen.Chat) }

            val permissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission()
            ) { /* 拒绝就拒绝：日历页与设置页都会再提示一次 */ }

            LaunchedEffect(Unit) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                }
            }

            // 外部送来的导出文件 → 直接进设置页的导入流程
            LaunchedEffect(incomingFile) {
                if (incomingFile != null) screen = Screen.Settings
            }

            BackHandler(enabled = screen != Screen.Chat) { screen = Screen.Chat }

            when (screen) {
                Screen.Chat -> ChatScreen(
                    vm = vm,
                    onOpenSettings = { screen = Screen.Settings },
                    onOpenMemory = { screen = Screen.Memory },
                    onOpenCalendar = { calendarVm.reload(); screen = Screen.Calendar },
                    onOpenImport = { screen = Screen.Settings },
                )
                Screen.Calendar -> CalendarScreen(
                    vm = calendarVm,
                    onBack = { vm.refreshSettings(); screen = Screen.Chat },
                )
                Screen.Memory -> MemoryScreen(vm = vm, onBack = { screen = Screen.Chat })
                Screen.Settings -> SettingsScreen(
                    onBack = { vm.refreshSettings(); screen = Screen.Chat },
                    incomingFile = incomingFile,
                    onFileConsumed = onFileConsumed,
                )
            }
        }
    }
}
