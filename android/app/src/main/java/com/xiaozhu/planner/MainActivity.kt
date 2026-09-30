package com.xiaozhu.planner

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.xiaozhu.planner.ui.chat.ChatScreen
import com.xiaozhu.planner.ui.chat.ChatViewModel
import com.xiaozhu.planner.ui.memory.MemoryScreen
import com.xiaozhu.planner.ui.settings.SettingsScreen

/**
 * P0/P1 的壳：三个页面（聊天 / 记忆 / 设置）。没有上导航库——几个页面用一个状态切换就够，
 * 少一个依赖少一处出错。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { PlannerApp() }
    }
}

private enum class Screen { Chat, Memory, Settings }

@Composable
fun PlannerApp() {
    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize()) {
            val vm: ChatViewModel = viewModel()
            var screen by remember { mutableStateOf(Screen.Chat) }

            BackHandler(enabled = screen != Screen.Chat) { screen = Screen.Chat }

            when (screen) {
                Screen.Chat -> ChatScreen(
                    vm = vm,
                    onOpenSettings = { screen = Screen.Settings },
                    onOpenMemory = { screen = Screen.Memory },
                )
                Screen.Memory -> MemoryScreen(vm = vm, onBack = { screen = Screen.Chat })
                Screen.Settings -> SettingsScreen(onBack = { vm.refreshSettings(); screen = Screen.Chat })
            }
        }
    }
}