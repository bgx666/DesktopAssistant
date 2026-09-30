package com.xiaozhu.planner.core.llm

/**
 * 密钥体检：**在发请求之前**把"一看就不对"的密钥拦下来，并说清怎么改。
 *
 * 起因是一个真实的报错：用户粘贴密钥时把说明文字也带进去了，
 * 于是发出去的请求头是 `Authorization: Bearer 你的...`，
 * OkHttp 抛的是 `Unexpected char 0x4f60 at 7 in Authorization value`——
 * 这话对用户毫无意义（0x4f60 是「你」的编码，索引 7 是密钥第一个字符）。
 *
 * HTTP 头只能放 ASCII，中文/换行/引号都会让请求直接构造失败。
 * 与其让底层库抛一句天书，不如在这里翻译成人话。
 */
object ApiKeyCheck {

    /** 密钥清洗：去掉粘贴时常带的垃圾（两端空白、引号、`Bearer ` 前缀、换行）。 */
    fun sanitize(raw: String): String {
        var key = raw.trim()
        // 复制粘贴经常带上包裹的引号
        key = key.trim('"', '\'', '“', '”', '‘', '’')
        // 有的地方复制出来自带 "Bearer " 前缀，再加一次就变成 Bearer Bearer xxx
        if (key.startsWith("Bearer ", ignoreCase = true)) key = key.substring(7).trim()
        // 换行会让整个头非法
        key = key.replace("\r", "").replace("\n", "").trim()
        return key
    }

    /**
     * 有问题就返回一句"该怎么办"，没问题返回 null。
     *
     * [forDisplay] 为 true 时用于设置页的即时提示（措辞更短）。
     */
    fun problem(rawKey: String): String? {
        val key = rawKey.trim()
        if (key.isEmpty()) return "还没填 API Key。"

        val badIndex = key.indexOfFirst { it.code !in 0x21..0x7E }
        if (badIndex >= 0) {
            val ch = key[badIndex]
            val where = "第 ${badIndex + 1} 个字符"
            val what = when {
                ch.code > 0x7F -> "「$ch」"
                ch == ' ' -> "空格"
                else -> "控制字符"
            }
            return "API Key 里出现了 $what（$where）。密钥是纯英文数字的一长串，" +
                "多半是粘贴时把说明文字也带进来了——清空这一栏，只粘密钥本身。"
        }

        if (key.contains(' ')) {
            return "API Key 里有空格，多半是粘贴时断行了。清空重粘一次。"
        }
        if (key.length < 16) {
            return "API Key 只有 ${key.length} 个字符，看着不像完整的密钥（一般 30 个以上）。"
        }
        if (key.startsWith("Bearer ", ignoreCase = true)) {
            return "API Key 前面多了「Bearer 」，只填密钥本身即可（前缀由程序加）。"
        }
        return null
    }

    /** 设置页用的一句话状态（没问题就给个正向确认）。 */
    fun statusForDisplay(rawKey: String): String? = problem(rawKey)
}
