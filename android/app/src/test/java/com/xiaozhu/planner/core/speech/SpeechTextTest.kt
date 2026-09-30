package com.xiaozhu.planner.core.speech

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 朗读前的文本清洗。每条规则都对应一种"读出来很怪"的真实情况——
 * 比如把 `（笑）` 念成"括号笑括号"、把 emoji 念成"笑脸"。
 */
class SpeechTextTest {

    @Test
    fun `markdown 链接只留文字`() {
        assertEquals("看这个文档", SpeechText.clean("看这个[文档](https://example.com/a)"))
    }

    @Test
    fun `括号内容不朗读`() {
        assertEquals("我明白了。", SpeechText.clean("我明白了。（笑）"))
        assertEquals("明天见。", SpeechText.clean("明天见。[备注]"))
        assertEquals("收到。", SpeechText.clean("收到。【系统提示】"))
        assertEquals("好的", SpeechText.clean("好的(ok)"))
    }

    @Test
    fun `嵌套括号也能清干净`() {
        // （笑（真的））这种：循环清理到没有括号对为止
        assertEquals("行。", SpeechText.clean("行。（笑（真的））"))
    }

    @Test
    fun `emoji 不朗读`() {
        assertEquals("好的", SpeechText.clean("好的😄"))
        assertEquals("出发", SpeechText.clean("出发🚀"))
        assertEquals("好", SpeechText.clean("好\u200D"))
    }

    @Test
    fun `markdown 符号被去掉`() {
        assertEquals("重点在这", SpeechText.clean("**重点**在这"))
        assertEquals("标题", SpeechText.clean("## 标题"))
        assertEquals("代码", SpeechText.clean("`代码`"))
        assertEquals("引用", SpeechText.clean("> 引用"))
    }

    @Test
    fun `多行合并成句号连接`() {
        assertEquals("第一点。第二点", SpeechText.clean("第一点\n第二点"))
    }

    @Test
    fun `多余空白被压缩`() {
        assertEquals("a b", SpeechText.clean("a    b"))
    }

    @Test
    fun `空输入返回空串`() {
        assertEquals("", SpeechText.clean(null))
        assertEquals("", SpeechText.clean(""))
        assertEquals("", SpeechText.clean("   \n  "))
    }

    @Test
    fun `超长文本被截断`() {
        val long = "字".repeat(3000)
        assertEquals(SpeechText.MAX_CHARS, SpeechText.clean(long).length)
    }

    @Test
    fun `清洗后没有可读的字才不值得朗读`() {
        // "（笑）" 清完是空 → 别为它花一次合成请求
        assertFalse(SpeechText.worthSpeaking("（笑）"))
        assertFalse(SpeechText.worthSpeaking("😄"))
        assertFalse(SpeechText.worthSpeaking(""))
        assertFalse(SpeechText.worthSpeaking("。。。"))
        // 短回复该念就念：按长度卡会让用户觉得"它有时候不出声"
        assertTrue(SpeechText.worthSpeaking("嗯"))
        assertTrue(SpeechText.worthSpeaking("好的"))
        assertTrue(SpeechText.worthSpeaking("**行**"))
    }

    @Test
    fun `中文标点与数字保留`() {
        assertEquals("20:00 开会，记得带电脑", SpeechText.clean("20:00 开会，记得带电脑"))
    }
}

/** MiMo 返回体解析：层级记错的表现是"静默没声音"，所以钉死在测试里。 */
class MiMoResponseTest {

    @Test
    fun `标准响应能取到 base64`() {
        val body = """
            {"choices":[{"message":{"role":"assistant","content":"",
            "audio":{"data":"UklGRg==","format":"wav","voice":"冰糖"}}}]}
        """.trimIndent()
        assertEquals("UklGRg==", MiMoResponse.audioBase64(body))
    }

    @Test
    fun `多余的字段不影响解析`() {
        val body = """{"id":"x","usage":{"tokens":3},"choices":[{"index":0,"message":{"audio":{"data":"AA=="}}}]}"""
        assertEquals("AA==", MiMoResponse.audioBase64(body))
    }

    @Test
    fun `结构不对返回 null 而不是抛异常`() {
        assertEquals(null, MiMoResponse.audioBase64("""{"choices":[]}"""))
        assertEquals(null, MiMoResponse.audioBase64("""{"choices":[{"message":{}}]}"""))
        assertEquals(null, MiMoResponse.audioBase64("""{"choices":[{"message":{"audio":{}}}]}"""))
        assertEquals(null, MiMoResponse.audioBase64("not json at all"))
        assertEquals(null, MiMoResponse.audioBase64(""))
    }

    @Test
    fun `data 为空串也算失败`() {
        assertEquals(null, MiMoResponse.audioBase64("""{"choices":[{"message":{"audio":{"data":""}}}]}"""))
    }

    @Test
    fun `错误响应能读出说明`() {
        val body = """{"error":{"message":"invalid api key","type":"auth"}}"""
        assertEquals("invalid api key", MiMoResponse.errorMessage(body))
        assertEquals(null, MiMoResponse.errorMessage("""{"choices":[]}"""))
    }
}
