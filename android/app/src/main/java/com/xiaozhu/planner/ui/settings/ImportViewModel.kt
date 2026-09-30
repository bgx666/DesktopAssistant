package com.xiaozhu.planner.ui.settings

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.xiaozhu.planner.core.migrate.ExportPayload
import com.xiaozhu.planner.core.migrate.ExportReader
import com.xiaozhu.planner.core.migrate.Importer
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
            val result = withContext(Dispatchers.IO) { runCatching { importer.apply(payload, wipeFirst) } }
            result.onSuccess { r ->
                pending = null
                val warn = r.warnings.take(3).joinToString("；")
                _state.update {
                    it.copy(
                        busy = false,
                        preview = null,
                        message = "导入完成：记忆节点 ${r.nodes} 个、对话 ${r.messages} 条、" +
                            "日程 ${r.calendar} 项。" + if (warn.isBlank()) "" else "（$warn）",
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
}
