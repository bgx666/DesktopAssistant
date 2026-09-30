package com.xiaozhu.planner.ui.images

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.xiaozhu.planner.core.image.ImagePreparer
import com.xiaozhu.planner.ui.chat.ImageViewer
import com.xiaozhu.planner.ui.theme.PlannerColors

/**
 * 图片管理页：看、找、删、导出。
 *
 * 版式：
 * ```
 * 图片
 * 共 128 张 · 342 MB / 建议上限 500 MB
 * [████████░░░░░░]  68%
 * [全部][本月][近三个月][今年]  [只看已不在]
 * [搜索当时说的话…]        [最新在前]
 * ┌───┬───┬───┐
 * │ ▣ │ ▣ │ ▣ │   三列缩略图，右下角是日期
 * ├───┼───┼───┤
 * └───┴───┴───┘
 * 已选 3 张 · 8 MB   [导出 zip] [删除] [清空选择]
 * ```
 *
 * 删除时界面上会写明"**只删图片，记忆里仍然记得**"——不写清楚，用户不敢删。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ImageGalleryScreen(
    vm: ImageGalleryViewModel,
    onBack: () -> Unit,
) {
    val state by vm.state.collectAsState()
    val items = remember(state) { vm.visible(state) }
    var viewerRef by remember { mutableStateOf<String?>(null) }

    val exporter = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/zip")
    ) { uri -> if (uri != null) vm.exportSelectedTo(uri) }

    if (viewerRef != null) {
        androidx.compose.ui.window.Dialog(
            onDismissRequest = { viewerRef = null },
            properties = androidx.compose.ui.window.DialogProperties(
                usePlatformDefaultWidth = false,
                decorFitsSystemWindows = false,
            ),
        ) {
            val url = remember(viewerRef) { ImageStoreDataUrl(viewerRef!!) }
            if (url != null) {
                ImageViewer(dataUrl = url, onDismiss = { viewerRef = null })
            } else {
                Box(
                    modifier = Modifier.fillMaxSize().background(Color(0xF2000000)),
                    contentAlignment = Alignment.Center,
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("这张图已经不在了", color = Color.White)
                        TextButton(onClick = { viewerRef = null }) { Text("关闭") }
                    }
                }
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("图片") },
                actions = { TextButton(onClick = onBack) { Text("返回") } },
            )
        }
    ) { pad ->
        Column(modifier = Modifier.fillMaxSize().padding(pad)) {
            Overview(state)

            // 筛选：时间范围
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState())
                    .padding(horizontal = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TimeRange.entries.forEach { range ->
                    FilterChip(
                        selected = state.range == range,
                        onClick = { vm.setRange(range) },
                        label = { Text(range.label) },
                    )
                }
                FilterChip(
                    selected = state.onlyMissing,
                    onClick = { vm.setOnlyMissing(!state.onlyMissing) },
                    label = { Text("只看已不在") },
                )
            }

            // 搜索 + 排序
            Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = state.query,
                    onValueChange = vm::setQuery,
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("搜当时说的话…", maxLines = 1) },
                    singleLine = true,
                    shape = RoundedCornerShape(12.dp),
                )
                TextButton(onClick = {
                    val next = SortMode.entries[(state.sort.ordinal + 1) % SortMode.entries.size]
                    vm.setSort(next)
                }) { Text(state.sort.label) }
            }

            if (state.loading) {
                Text(
                    "正在读取…",
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
            } else if (items.isEmpty()) {
                Text(
                    "这里还没有图片。你发过的图会出现在这里。",
                    style = MaterialTheme.typography.bodySmall,
                    color = PlannerColors.TextSoft,
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
            }

            LazyVerticalGrid(
                columns = GridCells.Fixed(3),
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = PaddingValues(12.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                items(items, key = { it.id }) { item ->
                    Thumb(
                        item = item,
                        selected = item.id in state.selected,
                        onClick = { vm.toggle(item.id) },
                        onOpen = { viewerRef = item.ref },
                    )
                }
            }

            state.message?.let { text ->
                Row(
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text,
                        style = MaterialTheme.typography.bodySmall,
                        color = PlannerColors.Primary,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = vm::consumeMessage) { Text("知道了") }
                }
            }

            // 底部：选择与操作
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    if (state.selected.isEmpty()) "共 ${items.size} 张"
                    else "已选 ${state.selected.size} 张 · ${mb(state.selectedBytes)}",
                    style = MaterialTheme.typography.labelLarge,
                    modifier = Modifier.weight(1f),
                )
                if (state.selected.isNotEmpty()) {
                    TextButton(onClick = {
                        val stamp = java.time.LocalDate.now().toString().replace("-", "")
                        exporter.launch("小助图片-$stamp.zip")
                    }) { Text("导出 zip") }
                    TextButton(onClick = vm::deleteSelected) { Text("删除") }
                    TextButton(onClick = vm::clearSelection) { Text("取消") }
                } else {
                    TextButton(onClick = vm::selectAllVisible) { Text("全选") }
                    TextButton(onClick = vm::selectOlderThanOneYear) { Text("一年前") }
                    if (state.missingCount > 0) {
                        TextButton(onClick = vm::selectAllMissing) { Text("已不在") }
                    }
                }
            }

            // 删除语义必须写明：不写清楚，用户不敢删
            Text(
                "删除只删图片文件。记忆里仍然记得那些时候发过照片（时间、当时说的话、摘要都在），" +
                    "只是以后没法再把画面调出来看。",
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
            )
        }
    }
}

/** 顶部概览：张数、占用、与建议上限的对比。 */
@Composable
private fun Overview(state: GalleryUiState) {
    Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp)) {
        Text(
            "共 ${state.totalCount} 张 · ${mb(state.totalBytes)} / 建议上限 ${mb(state.capBytes)}",
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.height(4.dp))
        LinearProgressIndicator(
            progress = {
                if (state.capBytes <= 0) 0f
                else (state.totalBytes.toFloat() / state.capBytes).coerceIn(0f, 1f)
            },
            modifier = Modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp)),
            color = if (state.overCap) PlannerColors.Danger else PlannerColors.Primary,
        )
        if (state.overCap) {
            Text(
                "已经超过建议上限了。可以在这里筛出旧图，先导出 zip 再删。",
                style = MaterialTheme.typography.bodySmall,
                color = PlannerColors.Danger,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
        if (state.missingCount > 0) {
            Text(
                "其中 ${state.missingCount} 张已经不在手机里了（记忆里还留着记录）。",
                style = MaterialTheme.typography.bodySmall,
                color = PlannerColors.TextSoft,
                modifier = Modifier.padding(top = 2.dp),
            )
        }
        if (state.pendingMigration > 0) {
            Text(
                "还有 ${state.pendingMigration} 条老图片在搬进文件存储（每次启动搬一点，不影响使用）。",
                style = MaterialTheme.typography.labelMedium,
                color = PlannerColors.TextSoft,
                modifier = Modifier.padding(top = 2.dp),
            )
        }
    }
}

/** 一个缩略图：选中打勾、右下角日期、失效的显示"已不在"。 */
@Composable
private fun Thumb(
    item: GalleryItem,
    selected: Boolean,
    onClick: () -> Unit,
    onOpen: () -> Unit,
) {
    val bitmap = remember(item.ref) {
        if (!item.available) null
        else runCatching {
            com.xiaozhu.planner.core.image.ImageStore.dataUrl(item.ref)
                ?.let { ImagePreparer.decodeDataUrl(it) }
        }.getOrNull()
    }

    Box(
        modifier = Modifier
            .aspectRatio(1f)
            .clip(RoundedCornerShape(10.dp))
            .background(PlannerColors.MemoryCard)
            .border(
                if (selected) 2.dp else 1.dp,
                if (selected) PlannerColors.Primary else Color(0x22000000),
                RoundedCornerShape(10.dp),
            )
            // 点一下 = 选中（要删的图通常一眼扫过去就选了）；长按看大图
            .clickable(onClick = onClick),
    ) {
        if (bitmap != null) {
            Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = item.said.ifBlank { "图片" },
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    "已不在",
                    style = MaterialTheme.typography.labelMedium,
                    color = PlannerColors.TextSoft,
                )
            }
        }

        // 右下角日期
        Box(
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .padding(4.dp)
                .clip(RoundedCornerShape(6.dp))
                .background(Color(0xB3000000))
                .padding(horizontal = 5.dp, vertical = 1.dp),
        ) {
            Text(
                item.ts.drop(5).take(5),
                style = MaterialTheme.typography.labelSmall,
                color = Color.White,
            )
        }

        // 选中标记
        if (selected) {
            Box(
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(4.dp)
                    .size(18.dp)
                    .clip(CircleShape)
                    .background(PlannerColors.Primary),
                contentAlignment = Alignment.Center,
            ) {
                Text("✓", style = MaterialTheme.typography.labelSmall, color = Color.White)
            }
        }

        // 看大图：单独一个小按钮（避免与"选中"手势打架）
        if (item.available) {
            Box(
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .padding(4.dp)
                    .clip(RoundedCornerShape(6.dp))
                    .background(Color(0xB3000000))
                    .clickable(onClick = onOpen)
                    .padding(horizontal = 6.dp, vertical = 1.dp),
            ) {
                Text("看", style = MaterialTheme.typography.labelSmall, color = Color.White)
            }
        }
    }
}

private fun ImageStoreDataUrl(ref: String): String? =
    runCatching { com.xiaozhu.planner.core.image.ImageStore.dataUrl(ref) }.getOrNull()

private fun mb(bytes: Long): String = when {
    bytes >= 1024L * 1024 * 1024 -> "%.1f GB".format(bytes / 1024.0 / 1024 / 1024)
    bytes >= 1024L * 1024 -> "%.0f MB".format(bytes / 1024.0 / 1024)
    else -> "%.0f KB".format(bytes / 1024.0)
}
