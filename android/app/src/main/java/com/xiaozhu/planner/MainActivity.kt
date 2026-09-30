package com.xiaozhu.planner

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.xiaozhu.planner.reminder.ReminderNotifications
import com.xiaozhu.planner.ui.calendar.CalendarScreen
import com.xiaozhu.planner.ui.calendar.CalendarViewModel
import com.xiaozhu.planner.ui.chat.ChatScreen
import com.xiaozhu.planner.ui.chat.ChatViewModel
import com.xiaozhu.planner.ui.memory.MemoryScreen
import com.xiaozhu.planner.ui.settings.SettingsScreen

/**
 * 壳：四个页面（聊天 / 日历 / 记忆 / 设置）。没有上导航库——几个页面一个状态切换就够，
 * 少一个依赖少一处出错。
 *
 * 通知权限在这里要一次：日历的"到点提醒"完全依赖它，用户拒绝的话提醒会静默消失，
 * 所以必须在启动时就问清楚，而不是等到第一条提醒悄悄不响。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ReminderNotifications.ensureChannel(this)
        setContent { PlannerApp() }
    }
}

private enum class Screen { Chat, Calendar, Memory, Settings }

@Composable
fun PlannerApp() {
    MaterialTheme {
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

            BackHandler(enabled = screen != Screen.Chat) { screen = Screen.Chat }

            when (screen) {
                Screen.Chat -> ChatScreen(
                    vm = vm,
                    onOpenSettings = { screen = Screen.Settings },
                    onOpenMemory = { screen = Screen.Memory },
                    onOpenCalendar = { calendarVm.reload(); screen = Screen.Calendar },
                )
                Screen.Calendar -> CalendarScreen(
                    vm = calendarVm,
                    onBack = { vm.refreshSettings(); screen = Screen.Chat },
                )
                Screen.Memory -> MemoryScreen(vm = vm, onBack = { screen = Screen.Chat })
                Screen.Settings -> SettingsScreen(onBack = { vm.refreshSettings(); screen = Screen.Chat })
            }
        }
    }
}
