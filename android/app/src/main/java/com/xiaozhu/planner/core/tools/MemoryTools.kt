package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.int
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.NodeRenderer
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * 按时间逐层下钻回忆：从根（或指定节点）往下展开，叶子能看到当时的原文。
 */
class ExploreMemoryTreeTool(private val store: MemoryStore) : Tool {

    override val name = "explore_memory_tree"

    override val description =
        "翻阅记忆树，查看某个历史节点下的记录。回忆过去说过的话、做过的决定时用。" +
            "策略：先想清楚大概是什么时候；不确定就多搜几次或从根节点逐层往下，直到叶子（含原文）。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("node_id") {
                put("type", "string")
                put("description", "节点编号（node0_xxx / node1_xxx / node2_xxx）。留空或传 root 返回根节点概览。")
            }
        }
    }

    override suspend fun run(args: JsonObject): String {
        var id = args.str("node_id").orEmpty().trim()
        if (id.isEmpty() || id.equals("root", ignoreCase = true) || id == "根") {
            id = store.rootId() ?: return "记忆树还没有节点（对话尚未触发压缩）。"
        }
        val info = store.nodeInfo(id) ?: return "找不到节点 $id 的记录。"
        return NodeRenderer.renderForTool(info)
    }
}

/**
 * 内容检索：**只记得内容、不记得时间**时用它——这是 PC 版缺的那一环。
 */
class SearchMemoryTool(private val store: MemoryStore) : Tool {

    override val name = "search_memory"

    override val description =
        "全文检索记忆（包括压进叶子里的原文）。适合「那个餐厅叫什么」「我说过的那本书」这类问题。" +
            "命中后可以用 explore_memory_tree 看某段的原文。不要用猜时间去代替搜索。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("query") {
                put("type", "string")
                put("description", "关键词，1-5 个词（多个词是「都要命中」的关系）")
            }
            putJsonObject("limit") {
                put("type", "integer")
                put("description", "最多返回几个节点，默认 5")
            }
        }
        putJsonArray("required") { add(JsonPrimitive("query")) }
    }

    override suspend fun run(args: JsonObject): String {
        val query = args.str("query") ?: return "缺少参数 query。"
        val limit = args.int("limit", 5).coerceIn(1, 20)
        val hits = store.search(query, limit)
        if (hits.isEmpty()) return "记忆里没有找到「$query」。可以换个说法再搜。"
        return buildString {
            append("记忆检索「").append(query).append("」命中 ").append(hits.size).append(" 个节点：\n")
            hits.forEach { h ->
                append("- ").append(h.nodeId).append("（level ").append(h.level)
                h.timeStart?.takeIf { it.isNotBlank() }?.let { append("，").append(it.take(16)) }
                append("）：").append(h.snippet).append('\n')
            }
            append("要看某段原文，用 explore_memory_tree 传上面的节点编号。")
        }
    }
}