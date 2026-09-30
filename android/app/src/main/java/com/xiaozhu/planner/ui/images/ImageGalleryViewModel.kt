package com.xiaozhu.planner.ui.images

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.agent.ConversationEngine
import com.xiaozhu.planner.core.image.ImageStore
import com.xiaozhu.planner.core.memory.ImageHit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.LocalDate
import java.time.ZoneId

/** 一张图片在列表里的样子（含它在记忆里的索引信息）。 */
data class GalleryItem(
    val id: String,
    /** `sha1:…` 引用。 */
    val ref: String,
    val ts: String,
    val said: String,
    val nodeSummary: String,
    val bytes: Long,
    /** 文件还在不在（相册引用失效、被清理过就是 false）。 */
    val available: Boolean,
) {
    /** `yyyy-MM-dd`（按月份筛选用）。 */
    val day: String get() = ts.take(10)
}

data class GalleryUiState(
    val items: List<GalleryItem> = emptyList(),
    val selected: Set<String> = emptySet(),
    val loading: Boolean = true,
    val message: String? = null,
    /** 总张数与占用（含失效的）。 */
    val totalCount: Int = 0,
    val totalBytes: Long = 0L,
    val pendingMigration: Int = 0,
    /** 筛选条件。 */
    val range: TimeRange = TimeRange.ALL,
    val query: String = "",
    val sort: SortMode = SortMode.NEWEST,
    /** 只看失效的。 */
    val onlyMissing: Boolean = false,
    /** 建议上限（设置页与这里共用，超过就提醒，但**不自动删**）。 */
    val capBytes: Long = com.xiaozhu.planner.data.SettingsStore.DEFAULT_IMAGE_CAP,
) {
    val selectedBytes: Long get() = items.filter { it.id in selected }.sumOf { it.bytes }
    val overCap: Boolean get() = totalBytes > capBytes
    val missingCount: Int get() = items.count { !it.available }
}

enum class TimeRange(val label: String) {
    ALL("全部"),
    THIS_MONTH("本月"),
    LAST_3_MONTHS("近三个月"),
    THIS_YEAR("今年"),
}

enum class SortMode(val label: String) {
    NEWEST("最新在前"),
    OLDEST("最旧在前"),
    LARGEST("占用最大在前"),
}

/**
 * 图片管理页。
 *
 * 为什么要有这一页：图片占空间会一直涨，但**不做自动删除**（用户明确要求）——
 * 所以给一个能自己找、自己看、自己删的地方：筛选、搜索、多选、批量删、导出 zip。
 *
 * 一个必须说清的语义：**删图片 ≠ 删记忆**。删掉之后记忆里仍然记得"那天有过一张照片"
 * （时间、当时说的话、摘要都在），只是之后没法再把画面调出来看。
 */
class ImageGalleryViewModel(app: Application) : AndroidViewModel(app) {

    private val engine = ConversationEngine.get(app)

    private val _state = MutableStateFlow(GalleryUiState())
    val state: StateFlow<GalleryUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true) }
            val snapshot = withContext(Dispatchers.IO) {
                // 图片的"记忆索引"来自 list_images 的那套检索（buffer + 记忆树两条线）
                val hits: List<ImageHit> = engine.store.findImages(
                    fromMillis = null,
                    toMillis = null,
                    query = "",
                    limit = 500,
                )
                val byRef = hits.groupBy { it.ref }
                val files = ImageStore.all().associateBy { "sha1:" + it.nameWithoutExtension }
                val (count, bytes) = ImageStore.stats()
                val pending = runCatching {
                    com.xiaozhu.planner.core.image.ImageMigration.pendingCount(engine.db)
                }.getOrDefault(0)

                // 以文件为准列出来；没有索引信息的（比如直接放进目录的）也要显示
                val items = files.map { (ref, file) ->
                    val hit = byRef[ref]?.firstOrNull()
                    GalleryItem(
                        id = ref,
                        ref = ref,
                        ts = hit?.ts ?: stampOf(file.lastModified()),
                        said = hit?.said.orEmpty(),
                        nodeSummary = hit?.nodeSummary.orEmpty(),
                        bytes = file.length(),
                        available = true,
                    )
                }.sortedByDescending { it.ts }

                // 记忆里引用了、但文件已经不在的（用户删过、或相册里删了）也要列出来
                val dangling = hits.filter { it.ref.isNotBlank() && !files.containsKey(it.ref) }
                    .distinctBy { it.ref }
                    .map { hit ->
                        GalleryItem(
                            id = hit.ref,
                            ref = hit.ref,
                            ts = hit.ts,
                            said = hit.said,
                            nodeSummary = hit.nodeSummary,
                            bytes = 0,
                            available = false,
                        )
                    }

                Triple(items + dangling, count to bytes, pending)
            }
            _state.update {
                it.copy(
                    items = snapshot.first,
                    totalCount = snapshot.second.first,
                    totalBytes = snapshot.second.second,
                    pendingMigration = snapshot.third,
                    loading = false,
                )
            }
        }
    }

    // ── 筛选与排序（纯界面状态）────────────────────────────

    fun setRange(range: TimeRange) = _state.update { it.copy(range = range) }
    fun setQuery(query: String) = _state.update { it.copy(query = query) }
    fun setSort(sort: SortMode) = _state.update { it.copy(sort = sort) }
    fun setOnlyMissing(only: Boolean) = _state.update { it.copy(onlyMissing = only) }
    fun setCap(bytes: Long) = _state.update { it.copy(capBytes = bytes) }

    /** 按当前筛选条件算出来的列表（界面直接用这个）。 */
    fun visible(state: GalleryUiState): List<GalleryItem> {
        val today = LocalDate.now(ZONE)
        val from = when (state.range) {
            TimeRange.ALL -> null
            TimeRange.THIS_MONTH -> today.withDayOfMonth(1).toString()
            TimeRange.LAST_3_MONTHS -> today.minusMonths(3).toString()
            TimeRange.THIS_YEAR -> today.withDayOfYear(1).toString()
        }
        return state.items
            .filter { item ->
                (from == null || item.day >= from) &&
                    (state.query.isBlank() ||
                        item.said.contains(state.query, ignoreCase = true) ||
                        item.nodeSummary.contains(state.query, ignoreCase = true) ||
                        item.ts.contains(state.query)) &&
                    (!state.onlyMissing || !item.available)
            }
            .let { list ->
                when (state.sort) {
                    SortMode.NEWEST -> list.sortedByDescending { it.ts }
                    SortMode.OLDEST -> list.sortedBy { it.ts }
                    SortMode.LARGEST -> list.sortedByDescending { it.bytes }
                }
            }
    }

    // ── 选择 ────────────────────────────────────────────────

    fun toggle(id: String) = _state.update {
        it.copy(selected = if (id in it.selected) it.selected - id else it.selected + id)
    }

    fun clearSelection() = _state.update { it.copy(selected = emptySet()) }

    fun selectAllVisible() = _state.update { current ->
        current.copy(selected = visible(current).map { it.id }.toSet())
    }

    /** 选中某个月的全部（"按月份清理"用）。 */
    fun selectMonth(month: String) = _state.update { current ->
        val ids = visible(current).filter { it.day.startsWith(month) }.map { it.id }.toSet()
        current.copy(selected = current.selected + ids)
    }

    /** 选中一年前的全部。 */
    fun selectOlderThanOneYear() = _state.update { current ->
        val cutoff = LocalDate.now(ZONE).minusYears(1).toString()
        val ids = visible(current).filter { it.day < cutoff }.map { it.id }.toSet()
        current.copy(selected = current.selected + ids)
    }

    fun selectAllMissing() = _state.update { current ->
        current.copy(selected = current.selected + current.items.filter { !it.available }.map { it.id })
    }

    // ── 删除 ────────────────────────────────────────────────

    /**
     * 删除选中的图片文件。
     *
     * **只删文件，不动记忆**：节点里的引用会变成"已不在"，
     * 但时间、当时说的话、摘要都还在——所以之后仍然答得出"那天有过一张天空的照片"。
     */
    fun deleteSelected() {
        val ids = _state.value.selected
        if (ids.isEmpty()) {
            _state.update { it.copy(message = "还没有选中任何图片。") }
            return
        }
        viewModelScope.launch {
            val removed = withContext(Dispatchers.IO) {
                ids.count { id -> runCatching { ImageStore.delete(id) }.getOrDefault(false) }
            }
            _state.update {
                it.copy(
                    selected = emptySet(),
                    message = "已删除 $removed 张图片。" +
                        "记忆里仍然记得那些时候发过照片，只是画面调不出来了。",
                )
            }
            refresh()
        }
    }

    // ── 导出 zip ────────────────────────────────────────────

    /** 把选中的图片打包成 zip 写到 [uri]（删除前留个底）。 */
    fun exportSelectedTo(uri: android.net.Uri) {
        val ids = _state.value.selected
        if (ids.isEmpty()) {
            _state.update { it.copy(message = "还没有选中任何图片。") }
            return
        }
        viewModelScope.launch {
            _state.update { it.copy(message = "正在打包…") }
            val result = withContext(Dispatchers.IO) {
                runCatching {
                    val items = _state.value.items.filter { it.id in ids }
                    getApplication<Application>().contentResolver.openOutputStream(uri)?.use { out ->
                        java.util.zip.ZipOutputStream(out).use { zip ->
                            var index = 0
                            items.forEach { item ->
                                val file = ImageStore.fileOf(item.ref.substringAfter("sha1:"))
                                if (file == null) return@forEach
                                val safeDay = item.ts.take(10).ifBlank { "unknown" }
                                val said = item.said.take(12).replace(Regex("[\\\\/:*?\"<>|]"), "")
                                val name = buildString {
                                    append(safeDay).append('_').append(++index)
                                    if (said.isNotBlank()) append('_').append(said)
                                    append(".jpg")
                                }
                                zip.putNextEntry(java.util.zip.ZipEntry(name))
                                file.inputStream().use { it.copyTo(zip) }
                                zip.closeEntry()
                            }
                            // 附一份清单：时间 + 当时说的话，方便日后对照
                            zip.putNextEntry(java.util.zip.ZipEntry("清单.txt"))
                            zip.write(
                                items.joinToString("\n") { "${it.ts}\t${it.said}" }
                                    .toByteArray(Charsets.UTF_8)
                            )
                            zip.closeEntry()
                        }
                    } ?: throw IllegalStateException("写不进这个位置")
                    items.count { it.available }
                }
            }
            result.onSuccess { count ->
                _state.update { it.copy(message = "已导出 $count 张图片（含清单.txt）。") }
            }.onFailure { e ->
                _state.update { it.copy(message = "导出失败：${e.message}") }
            }
        }
    }

    fun consumeMessage() = _state.update { it.copy(message = null) }

    private fun stampOf(millis: Long): String =
        java.time.Instant.ofEpochMilli(millis).atZone(ZONE)
            .format(java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))

    private companion object {
        val ZONE: ZoneId = ZoneId.of("Asia/Shanghai")
    }
}
