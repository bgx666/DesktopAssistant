package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.memory.ImageHit
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * 把一张旧图**调出来给自己看**。
 *
 * 机制与 PC 的 `capture_screen` 一样：工具本身只把图放进"待注入"槽，
 * 由引擎在**下一次模型调用前**作为一条 user 消息注入图片块——于是模型真的看见它，
 * 然后回答"那天是阴天，云很厚"。
 *
 * 为什么不让 list_images 直接把图全塞进去：命中错了就是白烧 token
 * （每张至少 384 token），先列信息再决定看哪张更省。
 */
class ShowImageTool(
    private val stash: PendingImageStash,
    /** 按 id 找那张图（`list_images` 的检索结果里查）。**suspend**：要读库。 */
    private val lookup: suspend (String) -> ImageHit?,
) : Tool {

    override val name = "show_image"

    override val description =
        "把一张以前发过的图片调出来看（id 来自 list_images）。" +
            "调用后那张图的画面会出现在你下一轮里，你就能看到它的内容了。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("id") {
                put("type", "string")
                put("description", "list_images 给出的 id，例如 msg:42:0 或 node:node1_003:1")
            }
        }
        putJsonArray("required") { add(kotlinx.serialization.json.JsonPrimitive("id")) }
    }

    override suspend fun run(args: JsonObject): String {
        val id = args.str("id")
            ?: return "要给我一个 id（来自 list_images 的结果）。"
        val hit = lookup(id)
            ?: return "找不到 id=$id 的图片（可能已经被清理掉了）。可以再用 list_images 找一次。"
        if (hit.ref.isBlank()) return "这张图的记录还在，但图片本身已经不在了。"

        val ok = stash.offer(hit.ref, hit.ts)
        return if (ok) {
            "好，${hit.ts} 的那张图已经调出来了，画面在你下一轮就能看到。"
        } else {
            "这张图已经不在了（文件被清理掉或相册里删了）。" +
                "记忆里还记着那天有过一张图，但看不到内容了。"
        }
    }
}

/**
 * "下一轮要注入的图片"的暂存槽。
 *
 * 与 PC 的 `_pending_screenshots` 同构：工具写入、引擎在**下一次模型调用前**取走并清空
 * （取走即清空，避免陈旧图片一直被重发）。
 */
class PendingImageStash {

    private val pending = mutableListOf<Pair<String, String>>() // ref to 时间

    /** 放入一张待注入的图。图片取不出来（文件没了）返回 false。 */
    @Synchronized
    fun offer(ref: String, ts: String): Boolean {
        val available = com.xiaozhu.planner.core.image.ImageStore.exists(ref)
        if (!available) return false
        if (pending.none { it.first == ref }) pending += ref to ts
        return true
    }

    /** 取走全部待注入的图（取走即清空）。 */
    @Synchronized
    fun drain(): List<Pair<String, String>> {
        val taken = pending.toList()
        pending.clear()
        return taken
    }

    @Synchronized
    fun clear() = pending.clear()
}
