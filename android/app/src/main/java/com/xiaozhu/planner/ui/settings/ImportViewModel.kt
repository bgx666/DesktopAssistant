package com.xiaozhu.planner.ui.settings

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.migrate.ExportPayload
import com.xiaozhu.planner.core.migrate.ExportReader
import com.xiaozhu.planner.core.migrate.Importer
import com.xiaozhu.planner.core.migrate.SeedImport
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.InputStream

/**
 * 「导入旧数据」的状态机。
 *
 * 为什么要先预览再导入：这一步**会清空手机上已有的记忆**（用户同意的话），
 * 而记忆是这个 App 的全部价值所在。所以流程是
 * 选文件 → 报清两边各有多少 → 用户确认 → 才写。
 */
data class ImportUiState(
    val preview: Importer.Preview? = null,
    val busy: Boolean = false,
    val message: String? = null,
    val error: String? = null,
) {
    val hasPreview: Boolean get() = preview != null
}

class ImportViewModel(app: Application) : AndroidViewModel(app) {

    private val importer = Importer(PlannerDatabase.get(app))

    private val _state = MutableStateFlow(ImportUiState())
    val state: StateFlow<ImportUiState> = _state.asStateFlow()

    /** 暂存已读入的导出内容（确认后才写库）。 */
    private var pending: ExportPayload? = null

    fun load(open: () -> InputStream?) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, message = null, preview = null) }
            val result = withContext(Dispatchers.IO) {
                runCatching {
                    val stream = open() ?: throw ExportReader.BadFile("没能打开这个文件。")
                    val payload = stream.use { ExportReader.read(it) }
                    payload to importer.preview(payload)
                }
            }
            result.onSuccess { (payload, preview) ->
                pending = payload
                _state.update { it.copy(busy = false, preview = preview) }
            }.onFailure { e ->
                pending = null
                _state.update { it.copy(busy = false, error = e.message ?: "读取失败") }
            }
        }
    }

    fun confirm(wipeFirst: Boolean) {
        val payload = pending
        if (payload == null) {
            _state.update { it.copy(error = "还没有选文件。") }
            return
        }
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val backup = if (wipeFirst) backupBeforeWipe() else null
            val result = withContext(Dispatchers.IO) { runCatching { importer.apply(payload, wipeFirst) } }
            result.onSuccess { r ->
                pending = null
                val warn = r.warnings.take(3).joinToString("；")
                _state.update {
                    it.copy(
                        busy = false,
                        preview = null,
                        message = "导入完成：记忆节点 ${r.nodes} 个、对话 ${r.messages} 条、" +
                            "日程 ${r.calendar} 项。" + if (warn.isBlank()) "" else "（$warn）" +
                            (backup?.let { p -> "（导入前的数据已备份到 $p）" } ?: ""),
                    )
                }
            }.onFailure { e ->
                _state.update { it.copy(busy = false, error = "导入失败（没有改动任何数据）：${e.message}") }
            }
        }
    }

    fun dismiss() {
        pending = null
        _state.update { ImportUiState(message = it.message) }
    }

    /** 这个构建里有没有随包的旧数据（有才显示"导入随包数据"按钮）。 */
    fun hasBundledSeed(): Boolean = SeedImport.hasSeedAsset(getApplication())

    /**
     * 导出当前数据成一个 JSON 文件（备份 / 换机）。
     *
     * 格式与 PC 的导出脚本一致，所以两边能互相导。
     */
    fun exportTo(uri: android.net.Uri) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, message = null) }
            val result = withContext(Dispatchers.IO) {
                runCatching {
                    val payload = Importer(PlannerDatabase.get(getApplication())).export()
                    val text = com.xiaozhu.planner.core.llm.PlannerJson.encodeToString(
                        ExportPayload.serializer(), payload
                    )
                    getApplication<Application>().contentResolver.openOutputStream(uri)?.use {
                        it.write(text.toByteArray(Charsets.UTF_8))
                    } ?: throw IllegalStateException("写不进这个位置")
                    payload.counts
                }
            }
            result.onSuccess { counts ->
                _state.update {
                    it.copy(
                        busy = false,
                        message = "已备份：记忆节点 ${counts?.nodes ?: 0} 个、" +
                            "日程 ${counts?.calendarItems ?: 0} 项（含对话与运行状态）",
                    )
                }
            }.onFailure { e ->
                _state.update { it.copy(busy = false, error = "备份失败：${e.message}") }
            }
        }
    }

    /**
     * 破坏性导入之前**先在应用私有目录留一份备份**。
     *
     * 为什么要有：`导入并替换` 会清空手机上的数据，点错了就没法回头。
     * 备份放在 `files/backup/`（跟着应用数据走，卸载才没），
     * 路径会在结果里告诉用户，需要时能取出来。
     */
    private suspend fun backupBeforeWipe(): String? = withContext(Dispatchers.IO) {
        runCatching {
            val app = getApplication<Application>()
            val payload = Importer(PlannerDatabase.get(app)).export()
            val dir = java.io.File(app.filesDir, "backup").apply { mkdirs() }
            val stamp = java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")
                .format(java.time.LocalDateTime.now())
            val file = java.io.File(dir, "planner-$stamp.json")
            file.writeText(
                com.xiaozhu.planner.core.llm.PlannerJson.encodeToString(
                    ExportPayload.serializer(), payload
                )
            )
            file.absolutePath
        }.getOrNull()
    }

    /**
     * 导入**随包带进来的**旧数据（装机即迁移，不用传文件）。
     *
     * @param wipeFirst 用户明确要"用旧数据替换手机上现有的"时传 true。
     *        自动路径（启动时）永远不覆盖——见 [SeedImport.importIfNeeded]。
     */
    fun importBundled(wipeFirst: Boolean) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, message = null) }
            val backup = if (wipeFirst) backupBeforeWipe() else null
            val result = withContext(Dispatchers.IO) {
                runCatching {
                    SeedImport.importIfNeeded(
                        context = getApplication(),
                        db = PlannerDatabase.get(getApplication()),
                        force = wipeFirst,
                    )
                }
            }
            result.onSuccess { outcome ->
                _state.update {
                    it.copy(
                        busy = false,
                        message = when (outcome) {
                            is SeedImport.Outcome.Imported ->
                                "已导入随包的旧数据：记忆节点 ${outcome.nodes} 个、" +
                                    "对话 ${outcome.messages} 条、日程 ${outcome.calendar} 项。" +
                                    (backup?.let { p -> "（导入前的数据已备份到 $p）" } ?: "")
                            SeedImport.Outcome.Skipped -> "这个包里没有随包的旧数据（或已经导过了）。"
                            is SeedImport.Outcome.Failed -> "导入失败：${outcome.reason}"
                        },
                    )
                }
            }.onFailure { e ->
                _state.update { it.copy(busy = false, error = "导入失败：${e.message}") }
            }
        }
    }
}
