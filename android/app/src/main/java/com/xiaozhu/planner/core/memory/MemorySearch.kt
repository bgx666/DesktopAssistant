package com.xiaozhu.planner.core.memory

/**
 * 记忆检索：**不用 FTS**。
 *
 * 原因：SQLite 的 FTS4/FTS5 默认分词器（simple / unicode61）会把连续的汉字当成**一个整词**，
 * 搜"华为"命中不了"今晚华为那个聊完"这类子串——而小助的记忆几乎全是中文。
 * `LIKE '%关键词%'` 对中文恰好就是子串匹配，且不需要维护索引表（节点合并时也不用同步）。
 * 个人记忆量级（几千节点）下 LIKE 全表扫描是毫秒级，完全够用。
 *
 * 本文件是纯函数（SQL 拼装 + 片段提取），可 JVM 单测。
 */
object MemorySearch {

    /** 关键词拆分：空白切分、去空、去 LIKE 通配符、最多 5 个词。 */
    fun terms(query: String): List<String> =
        query.split(' ', '\t', '\n', '　', ',', '，')
            .map { it.replace("%", "").replace("_", "").trim() }
            .filter { it.isNotEmpty() }
            .distinct()
            .take(5)

    /** 生成"每个词都要命中（AND）"的查询：词可以出现在摘要、画像或叶子原文里。 */
    fun buildSql(characterId: String, queryTerms: List<String>, limit: Int): Pair<String, List<String>> {
        require(queryTerms.isNotEmpty()) { "至少要有一个关键词" }
        val where = queryTerms.joinToString(" AND ") { "(searchText LIKE ? OR summary LIKE ?)" }
        val sql = buildString {
            append("SELECT * FROM nodes WHERE characterId = ? AND (").append(where).append(")")
            append(" ORDER BY roundStart DESC, id LIMIT ?")
        }
        val args = mutableListOf<String>(characterId)
        queryTerms.forEach { t -> args += "%$t%"; args += "%$t%" }
        args += limit.toString()
        return sql to args
    }

    /**
     * 从节点文本里摘出命中片段（前后各留 [pad] 个字符，多词时取第一个命中的）。
     * 找不到就退回文本开头。
     */
    fun snippet(text: String, queryTerms: List<String>, pad: Int = 40, maxLen: Int = 120): String {
        if (text.isBlank()) return ""
        val flat = text.replace(Regex("\\s+"), " ").trim()
        val hit = queryTerms.firstNotNullOfOrNull { term ->
            flat.indexOf(term, ignoreCase = true).takeIf { it >= 0 }?.let { it to term.length }
        } ?: return flat.take(maxLen)

        val (idx, len) = hit
        val start = (idx - pad).coerceAtLeast(0)
        val end = (idx + len + pad).coerceAtMost(flat.length)
        val prefix = if (start > 0) "…" else ""
        val suffix = if (end < flat.length) "…" else ""
        return prefix + flat.substring(start, end) + suffix
    }

    /** 叶子原文 → 可检索文本（节点的 searchText 用它拼）。 */
    fun searchableText(summary: String, profile: ProfileInfo?, messages: List<com.xiaozhu.planner.core.llm.ChatMessage>): String =
        buildString {
            append(summary)
            profile?.let { p ->
                (p.preferences + p.personality + p.habits + p.goals).forEach { append('\n').append(it) }
            }
            messages.forEach { m -> m.content?.takeIf { it.isNotBlank() }?.let { append('\n').append(it) } }
        }.trim()
}