package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.int
import com.xiaozhu.planner.core.agent.str
import com.xiaozhu.planner.core.image.ImageStore
import com.xiaozhu.planner.core.memory.ImageHit
import com.xiaozhu.planner.core.memory.MemoryJson
import com.xiaozhu.planner.core.memory.MemoryStore
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * 找出以前发过的图片（只回**元信息**，不回图片本身）。
 *
 * 为什么要这个工具：图片本体不进记忆树（base64 会把节点撑大几百倍），
 * 但"9/27 那天用户发过一张天空的照片"这件事必须留着。用户过一阵子问
 * "那天的天空什么样"，模型先用这个工具按时间/关键词找到那张图，
 * 再用 `show_image` 真正看它。
 *
 * 检索线索有三样：时间、当时说的话、所属记忆节点的摘要。
 */
class ListImagesTool(private val store: MemoryStore) : Tool {

    override val name = "list_images"

    override val description =
        "查找以前发过的图片（只列出信息，不含图片本身）。" +
            "用户问起某天/某段时间发过的图、或某张图的内容时先用它，" +
            "再用 show_image 把要看的图调出来。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("date") {
                put("type", "string")
                put("description", "只看某一天，YYYY-MM-DD（可空）")
            }
            putJsonObject("start_date") {
                put("type", "string")
                put("description", "时间范围起，YYYY-MM-DD（可空）")
            }
            putJsonObject("end_date") {
                put("type", "string")
                put("description", "时间范围止，YYYY-MM-DD（可空）")
            }
            putJsonObject("query") {
                put("type", "string")
                put("description", "关键词，匹配当时说的话或记忆摘要里的字（可空）")
            }
            putJsonObject("limit") {
                put("type", "integer")
                put("description", "最多列几张，默认 10，最多 30")
            }
        }
    }

    override suspend fun run(args: JsonObject): String {
        val limit = args.int("limit", 10).coerceIn(1, 30)
        val query = args.str("query")?.trim().orEmpty()

        val single = args.str("date")?.let { parseDate(it) }
        val start = single ?: args.str("start_date")?.let { parseDate(it) }
        val end = single ?: args.str("end_date")?.let { parseDate(it) }

        val hits = store.findImages(
            fromMillis = start?.atStartOfDay(ZONE)?.toInstant()?.toEpochMilli(),
            toMillis = end?.plusDays(1)?.atStartOfDay(ZONE)?.toInstant()?.toEpochMilli()?.minus(1),
            query = query,
            limit = limit,
        )

        if (hits.isEmpty()) {
            val scope = buildString {
                if (start != null || end != null) append("这段时间内")
                if (query.isNotBlank()) append("跟「$query」有关的")
            }
            // 注意 ${scope}：Kotlin 允许中文做标识符，写成 "$scope图片" 会被解析成
            // 一个叫 `scope图片` 的变量（编译器报 Unresolved reference 'scope图片'）
            return if (scope.isEmpty()) "还没有找到任何图片。" else "没有找到${scope}图片。"
        }

        return buildString {
            append("找到 ").append(hits.size).append(" 张图片：\n")
            hits.forEach { hit ->
                // 用 ` | ` 分隔而不是全角空格：全角空格在正则里**不算空白**
                // （Java/Kotlin 的 \s 只认 ASCII 空白），解析时容易把后面的内容一起吃掉
                append("- id=").append(hit.id)
                append(" | 时间=").append(hit.ts)
                if (hit.said.isNotBlank()) append(" | 当时说：").append(hit.said.take(60))
                if (hit.nodeSummary.isNotBlank()) {
                    append(" | 所在记忆：").append(hit.nodeSummary.take(50))
                }
                append("\n")
            }
            append("（要用哪张就调 show_image(id=…)，它的画面会出现在你下一轮里）")
        }
    }

    private fun parseDate(text: String): LocalDate? =
        runCatching { LocalDate.parse(text.trim(), DATE) }.getOrNull()

    private companion object {
        val ZONE: ZoneId = ZoneId.of("Asia/Shanghai")
        val DATE: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd")
    }
}
