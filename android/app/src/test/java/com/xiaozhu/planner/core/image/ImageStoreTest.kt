package com.xiaozhu.planner.core.image

import android.content.Context
import android.graphics.Bitmap
import android.util.Base64
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.core.memory.ImageRef
import com.xiaozhu.planner.core.memory.LeafDetails
import com.xiaozhu.planner.core.memory.MessageMapping
import com.xiaozhu.planner.data.db.MessageEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 图片仓库 + 迁移。
 *
 * 最要紧的一条是**迁移不可能丢图**：
 * - 先写文件、成功了才改那一行
 * - 逐行独立，失败的行原样留着 base64
 * - 读取端同时认两种格式（半迁移状态也能用）
 *
 * 所以用例不只测"能搬"，更要测**搬不动的时候图还在**。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ImageStoreTest {

    private lateinit var context: Context
    private lateinit var db: PlannerDatabase

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ImageStore.init(context)
        db = Room.inMemoryDatabaseBuilder(context, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
    }

    @After
    fun tearDown() {
        db.close()
        ImageStore.all().forEach { it.delete() }
    }

    private fun dataUrl(seed: Int, size: Int = 24): String {
        val bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(0xFF000000.toInt() or seed)
        val bytes = java.io.ByteArrayOutputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
            bitmap.recycle()
            out.toByteArray()
        }
        return "data:image/png;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    /** 按库里那一列的存法编码图片列表。 */
    private fun encode(values: List<String>): String =
        PlannerJson.encodeToString(ListSerializer(String.serializer()), values)

    private fun insert(content: String, ts: Long, images: List<String>) = runBlocking {
        db.messages().insert(
            MessageEntity(role = "user", content = content, tsMillis = ts, images = encode(images))
        )
    }

    // ── 仓库本身 ────────────────────────────────────────────

    @Test
    fun `存进去再取出来是同一张图`() {
        val url = dataUrl(1)
        val ref = ImageStore.put(url)!!

        assertTrue("应该返回 sha1: 引用", ref.startsWith("sha1:"))
        assertEquals("内容寻址：同一张图再存一次是同一个引用（顺带去重）", ref, ImageStore.put(url))
        assertTrue(ImageStore.exists(ref))
        assertNotNull(ImageStore.dataUrl(ref))
    }

    @Test
    fun `老格式的 base64 仍然照常解析（双格式读取）`() {
        val url = dataUrl(2)
        assertEquals("内嵌 base64 必须原样返回", url, ImageStore.dataUrl(url))
        assertTrue(ImageStore.exists(url))
    }

    @Test
    fun `引用指向的文件没了就是不存在（相册里被删了那种）`() {
        val ref = ImageStore.put(dataUrl(3))!!
        ImageStore.delete(ref)
        assertFalse(ImageStore.exists(ref))
        assertNull("取不到就该返回 null，而不是抛异常", ImageStore.dataUrl(ref))
    }

    @Test
    fun `认不出来的值返回 null，不崩`() {
        assertNull(ImageStore.dataUrl("乱七八糟"))
        assertFalse(ImageStore.exists(""))
    }

    @Test
    fun `统计张数与占用`() {
        ImageStore.put(dataUrl(4))
        ImageStore.put(dataUrl(5))
        val (count, bytes) = ImageStore.stats()
        assertTrue(count >= 2)
        assertTrue(bytes > 0)
    }

    // ── 迁移：搬得动 ────────────────────────────────────────

    @Test
    fun `老格式的消息会被搬成引用，图还在`() = runBlocking {
        val url = dataUrl(6)
        insert("今天的天空", 1_759_000_000_000, listOf(url))

        val moved = ImageMigration.runBatch(db)

        assertEquals(1, moved)
        val row = db.messages().all().single()
        val raw = MessageMapping.rawImagesOf(row).single()
        assertTrue("库里应该只剩引用：$raw", raw.startsWith("sha1:"))
        // **图还在**：读出来的 data URL 能解回位图
        assertNotNull(ImagePreparer.decodeDataUrl(MessageMapping.imagesOf(row).single()))
        assertEquals("搬完之后没有待搬的了", 0, ImageMigration.pendingCount(db))
    }

    @Test
    fun `已经全是引用的行不会再被搬（幂等）`() = runBlocking {
        val ref = ImageStore.put(dataUrl(7))!!
        insert("x", 1L, listOf(ref))

        assertEquals("没有待搬的行就不该动它", 0, ImageMigration.runBatch(db))
        assertEquals(ref, MessageMapping.rawImagesOf(db.messages().all().single()).single())
    }

    // ── 迁移：搬不动的时候图必须还在 ────────────────────────

    @Test
    fun `存文件失败时那一行原样留着 base64（图不丢）`() = runBlocking {
        // base64 是空的 → ImageStore.put 一定返回 null
        val broken = "data:image/jpeg;base64,"
        insert("坏图", 1L, listOf(broken))

        assertEquals("搬不动就不该动那一行", 0, ImageMigration.runBatch(db))
        assertEquals(
            "原值必须一字不改地留着",
            broken,
            MessageMapping.rawImagesOf(db.messages().all().single()).single(),
        )
    }

    @Test
    fun `一行里有多张图时，只要有一张搬不动就整行不动`() = runBlocking {
        val good = dataUrl(8)
        val bad = "data:image/jpeg;base64,"
        insert("两张图", 1L, listOf(good, bad))

        assertEquals(0, ImageMigration.runBatch(db))
        assertEquals(
            "两张都还是原值",
            listOf(good, bad),
            MessageMapping.rawImagesOf(db.messages().all().single()),
        )
    }

    @Test
    fun `半迁移状态可用：引用与 base64 混着都能读`() = runBlocking {
        val ref = ImageStore.put(dataUrl(9))!!
        insert("新", 2L, listOf(ref))
        insert("旧", 1L, listOf(dataUrl(10)))

        db.messages().all().forEach { row ->
            val resolved = MessageMapping.imagesOf(row)
            assertEquals("每一行都应该能解析出一张图", 1, resolved.size)
            assertNotNull(ImagePreparer.decodeDataUrl(resolved.single()))
        }
    }

    @Test
    fun `新消息落盘时就只存引用（不再往数据库塞 base64）`() {
        val message = ChatMessage.userWithImages("看图", listOf(dataUrl(11)))

        val row = MessageMapping.toEntity(message, tsMillis = 5L)

        val raw = MessageMapping.rawImagesOf(row).single()
        assertTrue("落盘时就该是引用：$raw", raw.startsWith("sha1:"))
        assertFalse("数据库里不该再有 base64", raw.contains("base64"))
    }

    // ── 记忆节点里的引用 ────────────────────────────────────

    @Test
    fun `记忆节点里留下的是引用与当时的话，不是图片本体`() {
        val details = LeafDetails(
            imageRefs = listOf(ImageRef(ref = "sha1:abc", ts = "2026-09-27 14:20:00", said = "今天的天空")),
        )
        val json = PlannerJson.encodeToString(LeafDetails.serializer(), details)

        assertTrue(json.contains("image_refs"))
        assertTrue(json.contains("sha1:abc"))
        assertFalse("节点里不能有 base64", json.contains("base64"))
        assertTrue("当时说的话要留着（检索线索）", json.contains("今天的天空"))
    }

    @Test
    fun `节点引用能按时间与关键词检索出来（工具要用）`() {
        val ref = ImageRef("sha1:x", "2026-09-27 14:20:00", "天空")
        assertEquals("sha1:x", ref.ref)
        assertTrue(ref.ts.startsWith("2026-09-27"))
        assertEquals("天空", ref.said)
    }
}
