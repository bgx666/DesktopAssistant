package com.xiaozhu.planner.core.image

import android.content.Context
import android.graphics.Bitmap
import android.util.Base64
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.agent.AgentLoop
import com.xiaozhu.planner.core.agent.ToolRegistry
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.LlmCompleter
import com.xiaozhu.planner.core.llm.LlmEvent
import com.xiaozhu.planner.core.llm.LlmStreamer
import com.xiaozhu.planner.core.llm.ToolSpec
import com.xiaozhu.planner.core.memory.CompressionSettings
import com.xiaozhu.planner.core.memory.Compressor
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.MessageMapping
import com.xiaozhu.planner.core.tools.ListImagesTool
import com.xiaozhu.planner.core.tools.PendingImageStash
import com.xiaozhu.planner.core.tools.ShowImageTool
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 端到端：**发一张图 → 被压缩进记忆树 → 之后还能把它找回来给模型看**。
 *
 * 前面几个测试各自覆盖一段（仓库、迁移、检索、注入），这一条把它们串起来跑，
 * 因为"整条链真的通"才是用户要的东西：
 *
 * > 我发了一个天空的图片，过一段时间再问他那天天气怎么样，
 * > 那天的文字可能有着记录，但我也希望图片也能够保留下来
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ImageRetrievalEndToEndTest {

    private lateinit var context: Context
    private lateinit var db: PlannerDatabase
    private lateinit var store: MemoryStore

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ImageStore.init(context)
        db = Room.inMemoryDatabaseBuilder(context, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        store = MemoryStore(db)
    }

    @After
    fun tearDown() {
        db.close()
        ImageStore.all().forEach { it.delete() }
    }

    private fun skyPhoto(): String {
        val bitmap = Bitmap.createBitmap(32, 32, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(0xFF87CEEB.toInt()) // 天蓝色
        val bytes = java.io.ByteArrayOutputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
            bitmap.recycle()
            out.toByteArray()
        }
        return "data:image/png;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    /** 压缩时用的假摘要模型：把"图片内容"写进摘要，就像视觉模型会做的那样。 */
    private class FakeSummarizer : LlmCompleter {
        var sawImages = false
            private set
        override suspend fun complete(messages: List<ChatMessage>): String {
            if (messages.any { it.images.isNotEmpty() }) sawImages = true
            return """{"summary":"9/27 用户发了张天空的照片，云很厚，是个阴天", """ +
                """"profile":{"preferences":["喜欢拍天空"]},"future_notes":[]}"""
        }
    }

    @Test
    fun `发图 → 压缩 → 之后仍能把那张图找回来给模型看`() = runBlocking {
        // ① 用户发了一张天空的照片
        val ts = java.time.LocalDateTime.parse("2026-09-27T14:20:00")
            .atZone(java.time.ZoneId.of("Asia/Shanghai")).toInstant().toEpochMilli()
        val message = ChatMessage.userWithImages("今天的天空", listOf(skyPhoto()))
        db.messages().insert(MessageMapping.toEntity(message, tsMillis = ts))

        // 落盘时就已经只存引用了（数据库里没有 base64）
        val stored = MessageMapping.rawImagesOf(db.messages().all().single()).single()
        assertTrue("库里应该是引用：$stored", stored.startsWith("sha1:"))

        // ② 攒够条数触发压缩：图片本体不进树，但引用与"当时说的话"留下
        repeat(30) { i ->
            db.messages().insert(
                com.xiaozhu.planner.data.db.MessageEntity(
                    role = "user", content = "闲聊 $i", tsMillis = ts + i * 60_000,
                )
            )
        }
        val summarizer = FakeSummarizer()
        val outcomes = Compressor(
            store = store,
            llm = summarizer,
            systemPrompt = "你是摘要器",
            settings = CompressionSettings(trigger = 10, keep = 5),
        ).compressIfNeeded()

        assertTrue("应该压出了节点：$outcomes", outcomes.any { it is com.xiaozhu.planner.core.memory.CompressionOutcome.LeafCreated })
        assertTrue("压缩时要把图片交给摘要模型（否则摘要里写不出图里是什么）", summarizer.sawImages)

        // 原消息已经被删掉——这就是"图片不在上下文里了"的状态
        val stillThere = db.messages().all().any { row ->
            MessageMapping.rawImagesOf(row).contains(stored)
        }
        assertFalse("原消息应该已经被压缩删除了", stillThere)

        // ③ 过了很久，用户问起那天的天空：list_images 找得到
        val listResult = ListImagesTool(store).run(buildJsonObject { put("query", "天空") })
        assertTrue("压缩后的图必须还能找到：$listResult", listResult.contains("找到 1 张图片"))
        assertTrue("摘要里的内容要能作为线索：$listResult", listResult.contains("云很厚"))
        val id = Regex("id=([A-Za-z0-9_:]+)").find(listResult)!!.groupValues[1]
        assertTrue("id 应指向记忆节点：$id", id.startsWith("node:"))

        // ④ show_image 把它调出来，下一轮模型调用里真的带上这张图
        val stash = PendingImageStash()
        val reply = ShowImageTool(stash) { store.findImageById(it) }.run(
            buildJsonObject { put("id", id) }
        )
        assertTrue("应该调出来了：$reply", reply.contains("已经调出来"))

        val llm = CapturingLlm()
        AgentLoop(
            llm = llm,
            registry = ToolRegistry(emptyList()),
            pendingImages = stash,
        ).run(mutableListOf(ChatMessage.system("S"), ChatMessage.user("那天的天空什么样"))).collect { }

        val sent = llm.sentMessages.first()
        val withImage = sent.lastOrNull { it.images.isNotEmpty() }
        assertNotNull("模型必须真的收到那张图", withImage)
        assertEquals("正好一张", 1, withImage!!.images.size)
        assertTrue("带的是同一张图", withImage.images.single().startsWith("data:image/"))
        assertTrue("要说明这是调出来的旧图", withImage.content!!.contains("show_image"))
    }

    @Test
    fun `按 id 精确取图，不受图片总数影响（不做全表扫描）`() = runBlocking {
        // 造 250 张图，全都压缩进节点
        val refs = (1..250).map { ImageStore.put(skyPhoto()) ?: "" }.distinct()
        assertTrue("内容相同会被去重成一张，这里只验证取图路径", refs.isNotEmpty())

        // 直接构造一个含多张引用的节点
        val nodeId = store.addLeaf(
            summary = "一堆照片",
            roundRange = 0 to 100,
            details = com.xiaozhu.planner.core.memory.LeafDetails(
                imageRefs = (1..20).map {
                    com.xiaozhu.planner.core.memory.ImageRef(
                        ref = refs.first(),
                        ts = "2026-09-27 14:20:00",
                        said = "第 $it 张",
                    )
                },
            ),
            timeRange = "2026-09-27 14:20:00" to "2026-09-27 14:20:00",
        )

        // 取最后一张（下标 19）：全表扫描的实现在图多了以后会漏，精确取不会
        val hit = store.findImageById("node:$nodeId:19")
        assertNotNull("应该能取到靠后的那一张", hit)
        assertEquals("第 20 张", hit!!.said)
        assertTrue(ImageStore.exists(hit.ref))

        // 越界与格式不对都返回 null，不崩
        assertEquals(null, store.findImageById("node:$nodeId:99"))
        assertEquals(null, store.findImageById("乱写"))
        assertEquals(null, store.findImageById("msg:99999:0"))
    }

    /** 记录每次 stream 收到的消息，第一轮就结束。 */
    private class CapturingLlm : LlmStreamer {
        val sentMessages = mutableListOf<List<ChatMessage>>()
        override fun stream(messages: List<ChatMessage>, tools: List<ToolSpec>): Flow<LlmEvent> = flow {
            sentMessages += messages
            emit(LlmEvent.TextDelta("那天是阴天"))
            emit(LlmEvent.Finished("stop"))
        }
    }

    /** 让编译器知道这个类型被用到了（JsonObject 在 buildJsonObject 里）。 */
    private fun unusedTypeAnchor(o: JsonObject) = o
}
