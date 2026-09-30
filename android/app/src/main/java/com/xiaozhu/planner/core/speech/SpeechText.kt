package com.xiaozhu.planner.core.speech

/**
 * 朗读前的文本清洗：**回复是给人听的，不是给眼睛看的**。
 *
 * 与 PC 版 `tts.py::clean_speech_text` 逐条对齐（两端的听感要一致）：
 * 链接留文字、括号内容不读（`（笑）`/`[备注]`/`【系统提示】`）、emoji 不读、
 * markdown 符号去掉、多行合并成句号连接。
 *
 * 纯函数，直接单测——这里的每条规则都对应一种"读出来很怪"的真实情况。
 */
object SpeechText {

    /** 单次合成上限（与 PC 版一致：长了模型会截断或超时）。 */
    const val MAX_CHARS = 2000

    private val MD_LINK = Regex("""\[([^\]]*)]\([^)]*\)""")

    /** 括号内容不朗读：支持中英文括号与方括号、书名号式的【】。 */
    private val PAREN = Regex("""[（(【\[][^（()【】\[\]）]*[)）】\]]""")

    private val EMOJI = Regex(
        "[" +
            "\uD83C\uDF00-\uD83E\uDFFF" +   // 扩展 pictographs 等（代理对区间）
            "\u2600-\u27BF" +               // 杂项符号 / 装饰符号
            "\uD83C\uDDE6-\uD83C\uDDFF" +   // 区域指示符（国旗）
            "\u2B00-\u2BFF" +               // 杂项符号和箭头
            "\u2190-\u21FF" +               // 箭头
            "\uFE00-\uFE0F" +               // 变体选择符
            "\uD83C\uDFFB-\uD83C\uDFFF" +   // 肤色修饰符
            "\u200D" +                      // ZWJ
            "]"
    )

    /** 行内 markdown 符号：去掉了才不会被读成"星号星号"。 */
    private val MARKDOWN = listOf("**", "```", "`", "#", "*", "_", "~", ">")

    fun clean(text: String?): String {
        if (text.isNullOrBlank()) return ""
        // 显式 orEmpty 而不是靠智能转换：text 是 String?，直接赋值会推出 String?
        // 类型，后面每个 replace 都要写 ?.，编译器还只报"候选都不适用"，很绕
        var t: String = text.orEmpty()

        t = MD_LINK.replace(t) { it.groupValues[1] }      // 链接保留文字

        // 括号内容循环清理，直到没有可匹配的括号对（支持嵌套）
        while (true) {
            val next = PAREN.replace(t, "")
            if (next == t) break
            t = next
        }

        t = EMOJI.replace(t, "")
        MARKDOWN.forEach { t = t.replace(it, "") }
        t = t.replace("\r", " ").replace("\n\n", "\n")
        t = Regex("""\s{2,}""").replace(t, " ")

        val lines = t.split("\n").map { it.trim() }.filter { it.isNotEmpty() }
        return lines.joinToString("。").take(MAX_CHARS)
    }

    /**
     * 值得朗读吗：清洗后**一个字都没有**才跳过（"（笑）"/纯 emoji/空串）。
     *
     * 阈值刻意定成"有没有可读的字"而不是"够不够长"：短回复（"嗯""行"）本来就该念出来，
     * 按长度卡会让用户觉得"它有时候不出声"。而合成一次的开销远小于这种困惑。
     */
    fun worthSpeaking(text: String?): Boolean = clean(text).any { it.isLetterOrDigit() }
}
