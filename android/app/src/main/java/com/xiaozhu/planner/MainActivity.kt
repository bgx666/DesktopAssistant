package com.xiaozhu.planner

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * P0 骨架：只验证「工程能构建、App 能起、Compose 能渲染」。
 * 下一步才是流式对话（core/llm + core/agent）。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { PlannerApp() }
    }
}

@Composable
fun PlannerApp() {
    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize()) {
            Column(modifier = Modifier.padding(24.dp)) {
                Text("小助 · Android", style = MaterialTheme.typography.headlineSmall)
                Text(
                    "P0 骨架已跑通。接下来接入：流式对话、本地记忆树、日历提醒。",
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(top = 12.dp)
                )
            }
        }
    }
}