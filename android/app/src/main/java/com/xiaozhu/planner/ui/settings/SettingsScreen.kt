package com.xiaozhu.planner.ui.settings

import android.app.Application
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.xiaozhu.planner.data.SettingsStore
import com.xiaozhu.planner.reminder.ReminderNotifications

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(onBack: () -> Unit) {
    val app = LocalContext.current.applicationContext as Application
    val store = remember { SettingsStore(app) }

    var apiKey by remember { mutableStateOf(store.apiKey) }
    var baseUrl by remember { mutableStateOf(store.baseUrl) }
    var model by remember { mutableStateOf(store.model) }
    var saved by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("设置") },
                actions = { TextButton(onClick = onBack) { Text("返回") } },
            )
        }
    ) { pad ->
        Column(
            modifier = Modifier
                .padding(pad)
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Text("模型服务", style = MaterialTheme.typography.titleMedium)
            Text(
                "密钥用 Android 的加密存储保存，只留在本机，不上传。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
            )

            OutlinedTextField(
                value = apiKey,
                onValueChange = { apiKey = it; saved = false },
                label = { Text("API Key") },
                visualTransformation = PasswordVisualTransformation(),
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = baseUrl,
                onValueChange = { baseUrl = it; saved = false },
                label = { Text("Base URL") },
                singleLine = true,
                supportingText = { Text("默认 DeepSeek：${SettingsStore.DEFAULT_BASE}") },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
            OutlinedTextField(
                value = model,
                onValueChange = { model = it; saved = false },
                label = { Text("模型") },
                singleLine = true,
                supportingText = { Text("例如 deepseek-v4-flash-vision-exp") },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )

            Button(
                onClick = {
                    store.apiKey = apiKey
                    store.baseUrl = baseUrl
                    store.model = model
                    saved = true
                },
                modifier = Modifier.padding(top = 16.dp),
            ) { Text("保存") }

            if (saved) {
                Text(
                    "已保存。",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }

            // ── 提醒权限 ────────────────────────────────────────
            HorizontalDivider(modifier = Modifier.padding(vertical = 16.dp))
            Text("提醒权限", style = MaterialTheme.typography.titleMedium)
            Text(
                "日历到点提醒靠通知 + 精确闹钟。任一项没给，" +
                    "“晚上 8 点提醒我”就会要么不响、要么晚几分钟——" +
                    "而它仍然会说自己提醒了，所以这里要看一眼。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )

            val context = LocalContext.current
            val alarmManager = remember {
                context.getSystemService(android.content.Context.ALARM_SERVICE) as? android.app.AlarmManager
            }
            val notificationsOk = ReminderNotifications.canPost(context)
            val exactOk = Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
                (alarmManager?.canScheduleExactAlarms() ?: false)

            PermissionRow("通知", notificationsOk) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    context.startActivity(
                        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                }
            }
            PermissionRow("精确闹钟", exactOk) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    context.startActivity(
                        Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM)
                            .setData(android.net.Uri.parse("package:${context.packageName}"))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                }
            }
            Text(
                "没拿到精确闹钟时会退化成「大概这个点」，不精确但不会丢。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** 一行权限状态：绿＝已给，点了能跳去系统设置（多数权限只能在那儿改）。 */
@Composable
private fun PermissionRow(label: String, granted: Boolean, onOpen: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            if (granted) "✓" else "✗",
            style = MaterialTheme.typography.titleMedium,
            color = if (granted) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error,
        )
        Text(label, modifier = Modifier.weight(1f).padding(start = 8.dp))
        TextButton(onClick = onOpen, enabled = !granted) { Text("去设置") }
    }
}