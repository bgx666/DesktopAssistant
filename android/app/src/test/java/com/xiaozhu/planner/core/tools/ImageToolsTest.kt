package com.xiaozhu.planner.core.tools

import android.content.Context
import android.graphics.Bitmap
import android.util.Base64
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.image.ImageStore
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.memory.ImageRef
import com.xiaozhu.planner.core.memory.LeafDetails
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.memory.MessageMapping
import com.xiaozhu.planner.data.db.MessageEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 图片寻回：`list_images` 找得到、`show_image` 调得出来。
 *
 * 这条链路是"压缩之后还能回答'那天的天空什么样'"的关键——
 * 图片本体不在记忆树里，只有引用与当时的话，靠这两个工具把它找回来。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ImageToolsTest {

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

    private fun dataUrl(seed: Int): String {
        val bitmap = Bitmap.createBitmap(16, 16, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(0xFF000000.toInt() or seed)
        val bytes = java.io.ByteArrayOutputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
            bitmap.recycle()
            out.toByteArray()
        }
        return "data:image/png;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    private fun args(vararg pairs: Pair<String, String>): JsonObject = buildJsonObject {
        pairs.forEach { (k, v) -> put(k, v) }
    }

    private suspend fun insertImageMessage(
        content: String,
        ref: String,
        tsMillis: Long,
    ): Long = db.messages().insert(
        MessageEntity(
            role = "user",
            content = content,
            tsMillis = tsMillis,
            images = PlannerJson.encodeToString(ListSerializer(String.serializer()), listOf(ref)),
        )
    )

    /**
     * 2026-09-27 14:20（UTC+8）的时间戳。
     *
     * **算出来而不是手写常量**：我第一次手写的常量差了 20 天，用例就"找不到图"了——
     * 时间这种东西让代码算，别让脑子算。
     */
    private val sept27: Long = java.time.LocalDateTime.parse("2026-09-27T14:20:00")
        .atZone(java.time.ZoneId.of("Asia/Shanghai"))
        .toInstant()
        .toEpochMilli()

    // ── list_images ─────────────────────────────────────────

    @Test
    fun `按日期找得到 buffer 里的图`() = runBlocking {
        insertImageMessage("今天的天空真好看", ImageStore.put(dataUrl(1))!!, sept27)

        val result = ListImagesTool(store).run(args("date" to "2026-09-27"))

        assertTrue("应该找到：$result", result.contains("找到 1 张图片"))
        assertTrue("要给出当时说的话（检索线索）", result.contains("今天的天空真好看"))
        assertTrue("要给出 id 供 show_image 用", result.contains("id=msg:"))
        assertTrue("要提示下一步怎么调", result.contains("show_image"))
    }

    @Test
    fun `按关键词找得到`() = runBlocking {
        insertImageMessage("今天的天空真好看", ImageStore.put(dataUrl(2))!!, sept27)
        insertImageMessage("食堂的菜", ImageStore.put(dataUrl(3))!!, sept27 + 60_000)

        val result = ListImagesTool(store).run(args("query" to "天空"))

        assertTrue(result.contains("天空"))
        assertFalse("不相关的图不该出现", result.contains("食堂"))
    }

    @Test
    fun `日期不匹配就找不到`() = runBlocking {
        insertImageMessage("天空", ImageStore.put(dataUrl(4))!!, sept27)
        val result = ListImagesTool(store).run(args("date" to "2026-01-01"))
        assertTrue(result.contains("没有找到"))
    }

    @Test
    fun `已经压进记忆树的图也能找到（这是关键）`() = runBlocking {
        // 模拟：原消息早被压缩删掉，只剩节点里的引用
        val ref = ImageStore.put(dataUrl(5))!!
        store.addLeaf(
            summary = "用户那天发了张天空的照片，说云很厚",
            roundRange = 0 to 10,
            details = LeafDetails(
                imageRefs = listOf(ImageRef(ref = ref, ts = "2026-09-27 14:20:00", said = "今天的天空")),
            ),
            timeRange = "2026-09-27 14:20:00" to "2026-09-27 14:20:00",
        )

        val result = ListImagesTool(store).run(args("query" to "天空"))

        assertTrue("压缩后的图必须还能找到：$result", result.contains("找到 1 张图片"))
        assertTrue("要带上所属记忆的摘要", result.contains("云很厚"))
        assertTrue("id 要指向节点", result.contains("id=node:"))
    }

    @Test
    fun `没有图时给一句人话`() = runBlocking {
        assertTrue(ListImagesTool(store).run(args()).contains("还没有找到任何图片"))
    }

    // ── show_image ──────────────────────────────────────────

    @Test
    fun `show_image 把图放进待注入槽`() = runBlocking {
        val ref = ImageStore.put(dataUrl(6))!!
        insertImageMessage("天空", ref, sept27)
        val stash = PendingImageStash()
        val listResult = ListImagesTool(store).run(args("date" to "2026-09-27"))
        // id 只含字母数字与下划线冒号：别用 \S+，全角空格在 Java 正则里不算空白，
        // 会把后面的字段一起吃掉（第一次就是这么写错的）
        val id = Regex("id=([A-Za-z0-9_:]+)").find(listResult)!!.groupValues[1]

        val tool = ShowImageTool(stash) { wanted ->
            store.findImages(null, null, "", 50).firstOrNull { it.id == wanted }
        }
        val reply = tool.run(args("id" to id))

        assertTrue("要确认已调出来：$reply", reply.contains("已经调出来"))
        val drained = stash.drain()
        assertEquals("槽里应该正好有一张", 1, drained.size)
        assertEquals(ref, drained.single().first)
        assertTrue("取走即清空（不能一直被重发）", stash.drain().isEmpty())
    }

    @Test
    fun `id 不存在时说清楚`() = runBlocking {
        val stash = PendingImageStash()
        val tool = ShowImageTool(stash) { null }
        val reply = tool.run(args("id" to "msg:999:0"))
        assertTrue(reply.contains("找不到"))
        assertTrue(stash.drain().isEmpty())
    }

    @Test
    fun `图片文件已经不在时说清楚，而不是假装能看到`() = runBlocking {
        val ref = ImageStore.put(dataUrl(7))!!
        insertImageMessage("天空", ref, sept27)
        ImageStore.delete(ref) // 比如用户去图片页把它删了
        val stash = PendingImageStash()
        val tool = ShowImageTool(stash) { wanted ->
            store.findImages(null, null, "", 50).firstOrNull { it.id == wanted }
        }

        val reply = tool.run(args("id" to "msg:${db.messages().all().single().seq}:0"))

        assertTrue("要说清已经不在了：$reply", reply.contains("不在了"))
        assertTrue("不能往槽里放不存在的图", stash.drain().isEmpty())
    }

    @Test
    fun `不给 id 时提示要一个 id`() = runBlocking {
        val tool = ShowImageTool(PendingImageStash()) { null }
        assertTrue(tool.run(args()).contains("id"))
    }

    @Test
    fun `同一个 id 重复调用只放一张（不重复烧 token）`() {
        val stash = PendingImageStash()
        val ref = ImageStore.put(dataUrl(8))!!
        assertTrue(stash.offer(ref, "t1"))
        assertTrue(stash.offer(ref, "t2"))
        assertEquals(1, stash.drain().size)
    }

    // ── 注入：模型真的能看到 ────────────────────────────────

    @Test
    fun `注入的图片会作为一条带图消息出现在下一次模型调用里`() = runBlocking {
        val ref = ImageStore.put(dataUrl(9))!!
        val stash = PendingImageStash()
        stash.offer(ref, "2026-09-27 14:20:00")

        val llm = RecordingLlm()
        val history = mutableListOf(ChatMessage.system("S"), ChatMessage.user("那天的天空什么样"))
        AgentLoopForTest.run(llm, history, stash)

        val sent = llm.sentMessages.first()
        val injected = sent.lastOrNull { it.images.isNotEmpty() }
        assertTrue("必须有一条带图的消息被发出去", injected != null)
        assertEquals("带的就是那张图", 1, injected!!.images.size)
        assertTrue("要说明这是调出来的旧图", injected.content!!.contains("show_image"))
        assertTrue("要带上时间，模型才知道是哪天", injected.content!!.contains("2026-09-27"))
        assertTrue("取走即清空", stash.drain().isEmpty())
    }

    @Test
    fun `槽是空的时候不注入任何东西`() = runBlocking {
        val llm = RecordingLlm()
        val history = mutableListOf(ChatMessage.system("S"), ChatMessage.user("在吗"))
        AgentLoopForTest.run(llm, history, PendingImageStash())

        assertTrue(
            "不该凭空多出带图消息",
            llm.sentMessages.first().none { it.images.isNotEmpty() },
        )
    }

    /** 记录每次 stream 收到的消息，第一轮就结束。 */
    private class RecordingLlm : com.xiaozhu.planner.core.llm.LlmStreamer {
        val sentMessages = mutableListOf<List<ChatMessage>>()
        override fun stream(
            messages: List<ChatMessage>,
            tools: List<com.xiaozhu.planner.core.llm.ToolSpec>,
        ) = kotlinx.coroutines.flow.flow {
            sentMessages += messages
            emit(com.xiaozhu.planner.core.llm.LlmEvent.TextDelta("嗯"))
            emit(com.xiaozhu.planner.core.llm.LlmEvent.Finished("stop"))
        }
    }

    private object AgentLoopForTest {
        suspend fun run(
            llm: com.xiaozhu.planner.core.llm.LlmStreamer,
            history: MutableList<ChatMessage>,
            stash: PendingImageStash,
        ) {
            com.xiaozhu.planner.core.agent.AgentLoop(
                llm = llm,
                registry = com.xiaozhu.planner.core.agent.ToolRegistry(emptyList()),
                pendingImages = stash,
            ).run(history).collect { }
        }
    }
}
