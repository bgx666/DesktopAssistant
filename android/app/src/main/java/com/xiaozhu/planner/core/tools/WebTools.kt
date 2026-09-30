package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.Tool
import com.xiaozhu.planner.core.agent.int
import com.xiaozhu.planner.core.agent.str
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

private const val UA = "XiaozhuAndroid/0.1 (personal study assistant)"

/** 网页搜索：Bing RSS，短关键词，返回标题+链接+摘要。离线可用（只要有网）。 */
class WebSearchTool(
    private val client: OkHttpClient = defaultHttp(),
) : Tool {

    override val name = "web_search"

    override val description =
        "上网搜索（走 Bing RSS）。query 用 2-5 个具体关键词，不要整句问句；" +
            "拿到结果后再用 fetch_web 打开具体的网页读正文。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("query") {
                put("type", "string")
                put("description", "搜索关键词，2-5 个词，例如「小米 MiMo 语音 模型」")
            }
            putJsonObject("limit") {
                put("type", "integer")
                put("description", "返回条数，默认 5，最多 10")
            }
        }
        putJsonArray("required") { add(JsonPrimitive("query")) }
    }

    override suspend fun run(args: JsonObject): String = withContext(Dispatchers.IO) {
        val query = args.str("query") ?: return@withContext "缺少参数 query。"
        val limit = args.int("limit", 5).coerceIn(1, 10)
        val url = "https://www.bing.com/search?format=rss&count=$limit&q=" +
            URLEncoder.encode(query, "UTF-8")
        val request = Request.Builder().url(url).addHeader("User-Agent", UA).build()
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@withContext "搜索失败：HTTP ${response.code}"
                val body = response.body?.string().orEmpty()
                val hits = BingRss.parse(body, limit)
                if (hits.isEmpty()) return@withContext "没搜到「$query」的结果（换几个关键词再试）。"
                buildString {
                    append("「").append(query).append("」的结果（").append(hits.size).append(" 条）：\n")
                    hits.forEachIndexed { i, h ->
                        append(i + 1).append(". ").append(h.title).append('\n')
                        append("   ").append(h.url).append('\n')
                        if (h.snippet.isNotEmpty()) append("   ").append(h.snippet).append('\n')
                    }
                }
            }
        } catch (e: Exception) {
            "搜索失败：${e.javaClass.simpleName}: ${e.message}"
        }
    }
}

/** 抓网页正文（去标签），并把正文里的链接也带出来，方便模型继续往下点。 */
class WebFetchTool(
    private val client: OkHttpClient = defaultHttp(),
) : Tool {

    override val name = "fetch_web"

    override val description =
        "抓取一个网页的正文（自动去掉 HTML 标签），返回纯文本 + 【页面链接】列表。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            putJsonObject("url") { put("type", "string"); put("description", "完整网址，含 https://") }
            putJsonObject("max_chars") {
                put("type", "integer")
                put("description", "正文最多返回多少字符，默认 6000")
            }
        }
        putJsonArray("required") { add(JsonPrimitive("url")) }
    }

    override suspend fun run(args: JsonObject): String = withContext(Dispatchers.IO) {
        val url = args.str("url") ?: return@withContext "缺少参数 url。"
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            return@withContext "url 必须以 http:// 或 https:// 开头。"
        }
        val maxChars = args.int("max_chars", 6000).coerceIn(500, 20000)
        val request = Request.Builder().url(url).addHeader("User-Agent", UA).build()
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    return@withContext "抓取失败：HTTP ${response.code}（412 通常是反爬，换别的来源）。"
                }
                val raw = response.body?.string().orEmpty()
                val text = Html.toPlainText(raw, maxChars)
                val links = Html.extractLinks(raw, url, limit = 20)
                buildString {
                    append("【正文】\n").append(text)
                    if (links.isNotEmpty()) {
                        append("\n\n【页面链接】\n")
                        links.forEach { (t, u) -> append("- ").append(t).append("：").append(u).append('\n') }
                    }
                }
            }
        } catch (e: Exception) {
            "抓取失败：${e.javaClass.simpleName}: ${e.message}"
        }
    }
}

/** 工具共用的 HTTP 客户端（超时短一点，工具的调用不该无限等）。 */
fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
    .connectTimeout(20, TimeUnit.SECONDS)
    .readTimeout(30, TimeUnit.SECONDS)
    .callTimeout(45, TimeUnit.SECONDS)
    .build()