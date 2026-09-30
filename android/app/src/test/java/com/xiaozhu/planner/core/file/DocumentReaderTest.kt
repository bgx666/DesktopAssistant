package com.xiaozhu.planner.core.file

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * 本地文档附件。
 *
 * 安卓没有后端可以解析文件，所以这里的分档是**刻意的**：
 * 纯文本直读、docx/pptx/xlsx 抽 XML、PDF 渲染成图（交给视觉模型）、其它只报名字。
 * 关键是**不能假装读得了**——读不出来要说清楚，而不是塞一段乱码给模型。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class DocumentReaderTest {

    private val context: Context = ApplicationProvider.getApplicationContext()

    private fun uriOf(name: String, bytes: ByteArray): android.net.Uri {
        val file = File(context.cacheDir, name)
        file.writeBytes(bytes)
        return android.net.Uri.fromFile(file)
    }

    @Test
    fun `纯文本直读`() {
        val uri = uriOf("笔记.txt", "论文第三章要补一个消融实验".toByteArray())
        val result = DocumentReader.read(context, uri, "笔记.txt")

        assertTrue(result is DocumentReader.Result.Text)
        assertTrue((result as DocumentReader.Result.Text).body.contains("消融实验"))
        // 拼进消息时的形状要带文件名，模型才知道这是附件
        val rendered = DocumentReader.renderText(result)
        assertTrue(rendered.contains("【附件：笔记.txt】"))
        assertTrue(rendered.contains("【附件结束】"))
    }

    @Test
    fun `markdown 与代码文件也算文本`() {
        listOf("readme.md", "data.json", "main.py", "notes.csv").forEach { name ->
            val uri = uriOf(name, "hello".toByteArray())
            assertTrue("$name 应该按文本读", DocumentReader.read(context, uri, name) is DocumentReader.Result.Text)
        }
    }

    @Test
    fun `二进制文件不当文本读（免得塞乱码给模型）`() {
        val uri = uriOf("blob.bin", byteArrayOf(0x00, 0x01, 0x02, 0x00, 0x03))
        val result = DocumentReader.read(context, uri, "blob.bin")
        // .bin 不在文本扩展名里 → 直接判不支持
        assertTrue(result is DocumentReader.Result.Unsupported)
    }

    @Test
    fun `文本里的 NUL 字节也会被识破`() {
        // 扩展名是 .txt 但内容是二进制（改名骗不过去）
        val uri = uriOf("fake.txt", byteArrayOf(0x41, 0x00, 0x42, 0x00))
        val result = DocumentReader.read(context, uri, "fake.txt")
        assertTrue(result is DocumentReader.Result.Unsupported)
        assertTrue((result as DocumentReader.Result.Unsupported).reason.contains("二进制"))
    }

    @Test
    fun `超过 32KB 只读前 32KB 并说明`() {
        val big = ByteArray(40 * 1024) { 'a'.code.toByte() }
        val uri = uriOf("big.txt", big)
        val result = DocumentReader.read(context, uri, "big.txt") as DocumentReader.Result.Text
        assertTrue(result.body.contains("只读了前 32KB"))
        assertTrue(result.body.length < 40 * 1024)
    }

    @Test
    fun `docx 抽出正文（zip + 剥标签，不引依赖）`() {
        val xml = """
            <?xml version="1.0"?>
            <w:document xmlns:w="x"><w:body>
              <w:p><w:r><w:t>第一段：审稿意见</w:t></w:r></w:p>
              <w:p><w:r><w:t>第二段：要补实验</w:t></w:r></w:p>
            </w:body></w:document>
        """.trimIndent()
        val bytes = zipOf("word/document.xml" to xml.toByteArray())
        val uri = uriOf("论文.docx", bytes)

        val result = DocumentReader.read(context, uri, "论文.docx") as DocumentReader.Result.Text
        assertTrue(result.body.contains("第一段：审稿意见"))
        assertTrue(result.body.contains("第二段：要补实验"))
        // 段落之间要有换行，否则整篇挤成一行
        assertTrue("段落应该分行", result.body.lines().size >= 2)
        // XML 标签不能漏进去
        assertTrue("不该带 XML 标签", !result.body.contains("<w:"))
    }

    @Test
    fun `docx 里没有文字时说清楚（纯图片文档）`() {
        val bytes = zipOf("word/document.xml" to "<w:document/>".toByteArray())
        val uri = uriOf("扫描件.docx", bytes)
        val result = DocumentReader.read(context, uri, "扫描件.docx")
        assertTrue(result is DocumentReader.Result.Unsupported)
        assertTrue((result as DocumentReader.Result.Unsupported).reason.contains("没抽出文字"))
    }

    @Test
    fun `不认识的格式只报名字，不假装能读`() {
        val uri = uriOf("老文档.doc", byteArrayOf(0xD0.toByte(), 0xCF.toByte(), 0x11, 0xE0.toByte()))
        val result = DocumentReader.read(context, uri, "老文档.doc")
        assertTrue(result is DocumentReader.Result.Unsupported)
        val unsupported = result as DocumentReader.Result.Unsupported
        assertEquals("老文档.doc", unsupported.name)
        assertTrue("要说清读不了：${unsupported.reason}", unsupported.reason.contains("读不了"))
    }

    private fun zipOf(vararg entries: Pair<String, ByteArray>): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        ZipOutputStream(out).use { zip ->
            entries.forEach { (name, data) ->
                zip.putNextEntry(ZipEntry(name))
                zip.write(data)
                zip.closeEntry()
            }
        }
        return out.toByteArray()
    }
}
