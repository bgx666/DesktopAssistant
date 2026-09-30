package com.xiaozhu.planner.core.migrate

import android.content.Context
import com.xiaozhu.planner.data.db.PlannerDatabase

/**
 * 首次启动时自动导入**随包带进来的**旧数据。
 *
 * 场景：电脑上那份记忆与日历要搬到手机上，而"传文件 → 选文件 → 确认"对一个
 * 只用手机的人来说是多余的三步。把导出文件放进 `assets/seed/`，装机即完成迁移。
 *
 * 三条护栏：
 * 1. **只在库为空时导入**——否则第二次启动会把自己刚聊出来的内容再灌一遍（重复记忆）
 * 2. **导入过就记标记**——即使后来把记忆删空了，也不该"复活"旧数据
 * 3. **没有 seed 文件就静默跳过**——正常的开发/发布包里没有这个文件
 *
 * ⚠️ `assets/seed/` 是 **gitignore** 的：里面是真实对话，不进仓库、不进公开发布包。
 */
object SeedImport {

    /** 导出文件在 assets 里的路径（目录被 gitignore）。 */
    const val ASSET_PATH = "seed/planner-export.json"

    private const val KEY_DONE = "seed_imported"

    /**
     * 该不该导入（纯函数，好测）。
     *
     * 三个条件缺一不可：没导过、库是空的、包里确实带了 seed。
     */
    fun shouldImport(alreadyImported: Boolean, hasLocalData: Boolean, hasSeedAsset: Boolean): Boolean =
        !alreadyImported && !hasLocalData && hasSeedAsset

    sealed interface Outcome {
        /** 导入了：节点/对话/日程各多少。 */
        data class Imported(val nodes: Int, val messages: Int, val calendar: Int) : Outcome

        /** 不需要或没有 seed。 */
        object Skipped : Outcome

        /** 有 seed 但导入失败（已记标记，避免每次启动都重试）。 */
        data class Failed(val reason: String) : Outcome
    }

    /**
     * @param force 用户明确要求"把随包的旧数据导进来（清空现有的）"时传 true。
     *        自动路径永远传 false——**绝不覆盖用户已经在用的数据**。
     */
    suspend fun importIfNeeded(
        context: Context,
        db: PlannerDatabase,
        characterId: String = com.xiaozhu.planner.core.memory.MemoryStore.DEFAULT_CHARACTER,
        force: Boolean = false,
    ): Outcome {
        val prefs = context.getSharedPreferences("planner_state", Context.MODE_PRIVATE)
        val alreadyImported = prefs.getBoolean(KEY_DONE, false)
        val hasData = db.nodes().count(characterId) > 0 ||
            db.messages().rawCount() > 0 ||
            db.calendar().rangeAll("0000-01-01", "9999-12-31").isNotEmpty()
        val hasSeed = hasSeedAsset(context)

        if (!force && !shouldImport(alreadyImported, hasData, hasSeed)) {
            // 库里已有数据 / 已经导过 / 包里没带 seed：记标记后跳过
            if (!alreadyImported && hasData) prefs.edit().putBoolean(KEY_DONE, true).apply()
            return Outcome.Skipped
        }
        if (!hasSeed) return Outcome.Skipped

        val payload = runCatching {
            context.assets.open(ASSET_PATH).use { ExportReader.read(it) }
        }.getOrNull() ?: run {
            prefs.edit().putBoolean(KEY_DONE, true).apply()
            return Outcome.Skipped
        }

        return runCatching {
            val result = Importer(db).apply(payload, wipeFirst = force, characterId = characterId)
            prefs.edit().putBoolean(KEY_DONE, true).apply()
            Outcome.Imported(result.nodes, result.messages, result.calendar)
        }.getOrElse { e ->
            // 失败也记标记：每次启动重试一遍只会让用户觉得"它总在干点什么"
            prefs.edit().putBoolean(KEY_DONE, true).apply()
            Outcome.Failed(e.message ?: e.javaClass.simpleName)
        }
    }

    /** 这个构建里有没有随包的旧数据（设置页据此决定要不要显示"强制导入"）。 */
    fun hasSeedAsset(context: Context): Boolean =
        runCatching { context.assets.open(ASSET_PATH).use { true } }.getOrDefault(false)

    /**
     * 包里带了旧数据、但**没导进去**（库非空所以自动路径跳过了）。
     *
     * 为什么要问而不是静默跳过：跳过是对的（不能覆盖用户已聊的内容），
     * 但**一声不响**就糟了——用户根本不知道包里还带着旧数据、也不知道自己错过了什么。
     * 界面拿到 true 就该给一张卡片问一句。
     */
    suspend fun isPending(
        context: Context,
        db: PlannerDatabase,
        characterId: String = com.xiaozhu.planner.core.memory.MemoryStore.DEFAULT_CHARACTER,
    ): Boolean {
        val prefs = context.getSharedPreferences("planner_state", Context.MODE_PRIVATE)
        if (prefs.getBoolean(KEY_DONE, false)) return false
        if (!hasSeedAsset(context)) return false
        val hasData = db.nodes().count(characterId) > 0 ||
            db.messages().rawCount() > 0 ||
            db.calendar().rangeAll("0000-01-01", "9999-12-31").isNotEmpty()
        return hasData
    }

    /** 用户说"先不用"：记标记，别每次都问。 */
    fun dismiss(context: Context) {
        context.getSharedPreferences("planner_state", Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_DONE, true).apply()
    }

    /** 随包数据的规模（卡片上告诉用户"包里有什么"，而不是让他猜）。 */
    fun peekCounts(context: Context): Triple<Int, Int, Int>? = runCatching {
        val payload = context.assets.open(ASSET_PATH).use { ExportReader.read(it) }
        Triple(payload.nodes.size, payload.calendarItems.size, 0)
    }.getOrNull()
}
