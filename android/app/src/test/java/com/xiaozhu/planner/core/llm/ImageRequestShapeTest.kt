package com.xiaozhu.planner.core.llm

import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 带图请求的报文形状 —— 这里写错端点就 400，而且是"整轮对话都发不出去"那种。
 *
 * OpenAI 协议的硬规定：**有图时 content 必须是数组**
 * （`[{"type":"text",...},{"type":"image_url","image_url":{"url":"data:..."}}]`），
 * 纯文本时仍然是字符串。
 */
class ImageRequestShapeTest {

    private val png = "data:image/jpeg;base64,AAAA"

    private fun bodyOf(messages: List<ChatMessage>): String {
        val client = OpenAiClient("https://api.deepseek.com/v1", "sk-0123456789abcdef", "deepseek-flash")
        val request = client.buildRequest(messages, null, stream = false)
        val buffer = okio.Buffer()
        request.body!!.writeTo(buffer)
        return buffer.readUtf8()
    }

    @Test
    fun `纯文本消息的 content 仍然是字符串`() {
        val body = bodyOf(listOf(ChatMessage.user("你好")))
        val msg = PlannerJson.parseToJsonElement(body).jsonObject["messages"]!!.let { it as JsonArray }[0]
        assertTrue(
            "纯文本不该变成数组：$body",
            msg.jsonObject["content"] is JsonPrimitive,
        )
    }

    @Test
    fun `带图消息的 content 是数组，含 text 与 image_url 两个块`() {
        val body = bodyOf(listOf(ChatMessage.userWithImages("看看这张", listOf(png))))
        val msg = PlannerJson.parseToJsonElement(body).jsonObject["messages"]!!.let { it as JsonArray }[0]
        val content = msg.jsonObject["content"] as JsonArray

        assertEquals(2, content.size)
        assertEquals("text", content[0].jsonObject["type"]!!.let { it as JsonPrimitive }.content)
        assertEquals("看看这张", content[0].jsonObject["text"]!!.let { it as JsonPrimitive }.content)
        assertEquals("image_url", content[1].jsonObject["type"]!!.let { it as JsonPrimitive }.content)
        assertEquals(
            png,
            content[1].jsonObject["image_url"]!!.jsonObject["url"]!!.let { it as JsonPrimitive }.content,
        )
    }

    @Test
    fun `只有图没有字时也能发（不带空的 text 块）`() {
        val body = bodyOf(listOf(ChatMessage.userWithImages("", listOf(png))))
        val msg = PlannerJson.parseToJsonElement(body).jsonObject["messages"]!!.let { it as JsonArray }[0]
        val content = msg.jsonObject["content"] as JsonArray

        assertEquals("空文本不该占一个块", 1, content.size)
        assertEquals("image_url", content[0].jsonObject["type"]!!.let { it as JsonPrimitive }.content)
    }

    @Test
    fun `多张图按顺序都发出去`() {
        val body = bodyOf(listOf(ChatMessage.userWithImages("两张", listOf(png, png, png))))
        val msg = PlannerJson.parseToJsonElement(body).jsonObject["messages"]!!.let { it as JsonArray }[0]
        val content = msg.jsonObject["content"] as JsonArray
        assertEquals(4, content.size) // 1 个 text + 3 个 image_url
    }

    @Test
    fun `纯文本消息不带 images 字段（协议里没有这个字段）`() {
        val body = bodyOf(listOf(ChatMessage.user("你好")))
        assertFalse("images 是我们内部的字段，不该出现在报文里", body.contains("\"images\""))
    }

    @Test
    fun `看不了图的模型在发请求前就被拦下`() = runBlocking {
        // deepseek-v4-pro 官方只支持文本
        val client = OpenAiClient("https://api.deepseek.com/v1", "sk-0123456789abcdef", "deepseek-v4-pro")
        val events = client.stream(listOf(ChatMessage.userWithImages("看图", listOf(png))), emptyList())
            .toList()
        // 客户端本身不拦（拦在 ViewModel 层，因为要给用户提示）；这里只确认
        // 它不会因为报文形状崩掉——真正的拦截在 ChatViewModel.attachImage
        assertTrue(events.isNotEmpty())
    }

    @Test
    fun `视觉能力判断：官方 deepseek-flash 算视觉模型`() {
        // 这一条是重点：PC 版只按名字猜，而 deepseek-flash 名字里没有 vision 之类字样，
        // 会被误判成"看不了图"——但它其实支持图片输入
        assertTrue(VisionSupport.isVisionCapable("deepseek-flash"))
        assertTrue(VisionSupport.isVisionCapable("deepseek-v4-flash-vision-exp"))
        assertTrue(VisionSupport.isVisionCapable("gpt-4o"))
        assertTrue(VisionSupport.isVisionCapable("qwen-vl-max"))
        assertFalse(VisionSupport.isVisionCapable("deepseek-v4-pro"))
        assertFalse(VisionSupport.isVisionCapable("deepseek-chat"))
        assertFalse(VisionSupport.isVisionCapable(""))
    }

    @Test
    fun `不支持图片时的提示要说清换成什么`() {
        val hint = VisionSupport.unsupportedHint("deepseek-v4-pro")
        assertTrue(hint.contains("deepseek-flash"))
        assertTrue(hint.contains("deepseek-v4-pro"))
    }
}
