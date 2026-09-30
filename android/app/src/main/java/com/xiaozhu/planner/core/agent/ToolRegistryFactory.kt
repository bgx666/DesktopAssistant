package com.xiaozhu.planner.core.agent

import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.DndStore
import com.xiaozhu.planner.core.calendar.ReminderPlanner
import com.xiaozhu.planner.core.memory.ImageHit
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.tools.AddScheduleTool
import com.xiaozhu.planner.core.tools.CancelScheduleTool
import com.xiaozhu.planner.core.tools.ContinueSpeakingTool
import com.xiaozhu.planner.core.tools.ExploreMemoryTreeTool
import com.xiaozhu.planner.core.tools.HeartbeatPlanner
import com.xiaozhu.planner.core.tools.HeartbeatStore
import com.xiaozhu.planner.core.tools.HeartbeatTool
import com.xiaozhu.planner.core.tools.ListImagesTool
import com.xiaozhu.planner.core.tools.ListScheduleTool
import com.xiaozhu.planner.core.tools.PendingImageStash
import com.xiaozhu.planner.core.tools.SearchMemoryTool
import com.xiaozhu.planner.core.tools.ShowImageTool
import com.xiaozhu.planner.core.tools.SetDoNotDisturbTool
import com.xiaozhu.planner.core.tools.UpdateScheduleTool
import com.xiaozhu.planner.core.tools.WebFetchTool
import com.xiaozhu.planner.core.tools.WebSearchTool

/**
 * 工具清单的**唯一出处**（11 个常驻工具）。
 *
 * 为什么单独抽出来：`ConversationEngine` 直接 new 这些工具的话，
 * 想验"这 11 个 schema 网关收不收"就必须先把整个引擎搭起来——而引擎要读
 * 加密设置（Robolectric 里没有 AndroidKeyStore，直接抛 KeyStoreException），
 * 于是**最该被验的东西反而验不了**。抽成工厂后，测试用内存库 + 假实现
 * 就能拿到与线上**完全同一份**注册表。
 *
 * 改动这个清单时记得同步：
 * - `android/app/src/main/assets/prompts/system.md` 的「A. 常驻手机」那一段
 * - `scripts/check_android_prompt.py`（会核对 prompt 与注册表是否一致）
 */
object ToolRegistryFactory {

    fun create(
        store: MemoryStore,
        calendar: CalendarStore,
        reminderPlanner: ReminderPlanner,
        heartbeatStore: HeartbeatStore,
        heartbeatPlanner: HeartbeatPlanner? = null,
        dndStore: DndStore,
        pacing: SpeechPacing = SpeechPacing(),
        /** `show_image` 的暂存槽（引擎与 AgentLoop 共用同一个实例）。 */
        pendingImages: PendingImageStash = PendingImageStash(),
    ): ToolRegistry = ToolRegistry(
        listOf(
            WebSearchTool(),
            WebFetchTool(),
            HeartbeatTool(heartbeatStore, heartbeatPlanner),
            ContinueSpeakingTool(pacing),
            ExploreMemoryTreeTool(store),
            SearchMemoryTool(store),
            // 图片寻回：先列信息，再决定看哪张（每张图至少 384 token，不能瞎塞）
            ListImagesTool(store),
            ShowImageTool(pendingImages) { id -> lookupImage(store, id) },
            ListScheduleTool(calendar),
            AddScheduleTool(calendar, reminderPlanner),
            UpdateScheduleTool(calendar, reminderPlanner),
            CancelScheduleTool(calendar, reminderPlanner),
            SetDoNotDisturbTool(dndStore),
        )
    )

    /**
     * 按 `list_images` 给出的 id 找那一张图（id 形如 `msg:42:0` 或 `node:node1_003:1`）。
     *
     * 走 [MemoryStore.findImageById] 精确取，**不做全表扫描**：
     * 图片多了以后"扫前 N 条再匹配"会漏掉靠后的那些。
     */
    private suspend fun lookupImage(store: MemoryStore, id: String): ImageHit? =
        store.findImageById(id)
}
