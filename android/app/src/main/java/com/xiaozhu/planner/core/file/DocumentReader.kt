package com.xiaozhu.planner.core.file

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.pdf.PdfRenderer
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.util.zip.ZipInputStream

/**
 * 本地文档附件。
 *
 * PC 版是拖文件给后端：`text` 类直接注入正文、`doc` 类（pdf/docx/doc）由后端解析、
 * 图片走视觉模型。**安卓没有后端可以解析**，所以按"能做到什么"分三档，
 * 并且**明确告诉用户这一档是怎么处理的**——不假装都能读：
 *
 * | 类型 | 做法 |
 * |---|---|
 * | 纯文本（txt/md/json/csv/代码…） | 直接读正文注入（≤32KB，与 PC 同一道闸门） |
 * | docx / pptx / xlsx | 本质是 zip，抽出里面的 XML 再剥标签取文字（不引依赖） |
 * | pdf | **渲染成图片**交给视觉模型（`PdfRenderer`，无需依赖） |
 * | 其它（doc/二进制） | 只把文件名告诉它，并说明读不了 |
 *
 * 为什么不引 PDFBox/POI：那是十几 MB 的依赖，而这个 App 的核心是对话，
 * 为附件解析背这么大的包不划算。渲染成图对视觉模型反而更忠实（版式、表格都在）。
 */
object DocumentReader {

    /** 纯文本直读上限（与 PC 的 `size <= 32768` 同一道闸门）。 */
    const val MAX_TEXT_BYTES = 32 * 1024

    /** PDF 最多渲染几页（每页一张图，多了又慢又贵）。 */
    const val MAX_PDF_PAGES = 3

    /** 文本类扩展名（PC 那份清单的精简版：手机上常见的那些）。 */
    private val TEXT_EXTENSIONS = setOf(
        "txt", "md", "markdown", "json", "csv", "tsv", "log", "yml", "yaml", "toml", "ini",
        "xml", "html", "htm", "css", "js", "ts", "jsx", "tsx", "py", "java", "kt", "kts",
        "c", "h", "cpp", "hpp", "cs", "go", "rs", "rb", "php", "sh", "ps1", "bat", "sql",
    )

    private val ZIP_DOC_EXTENSIONS = setOf("docx", "pptx", "xlsx")

    sealed interface Result {
        /** 读成正文了（会拼进这条消息的文字里）。 */
        data class Text(val name: String, val body: String) : Result

        /** 读成图片了（交给视觉模型，和拍照/相册同一条路）。 */
        data class Images(val name: String, val dataUrls: List<String>) : Result

        /** 读不了：只报个名字，并说明原因。 */
        data class Unsupported(val name: String, val reason: String) : Result
    }

    fun read(context: Context, uri: Uri, displayName: String?): Result {
        val name = displayName?.takeIf { it.isNotBlank() }
            ?: uri.lastPathSegment?.substringAfterLast('/') ?: "附件"
        val ext = name.substringAfterLast('.', "").lowercase()

        return when {
            ext == "pdf" -> readPdf(context, uri, name)
            ext in ZIP_DOC_EXTENSIONS -> readZipDoc(context, uri, name, ext)
            ext in TEXT_EXTENSIONS || ext.isEmpty() -> readText(context, uri, name)
            else -> Result.Unsupported(name, "这种格式（.$ext）在手机上读不了，只把文件名告诉了它。")
        }
    }

    private fun readText(context: Context, uri: Uri, name: String): Result = runCatching {
        val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
            ?: return Result.Unsupported(name, "打不开这个文件。")
        // 与 PC 同一道闸门：太大就只给前 32KB，并说明被截断了
        val truncated = bytes.size > MAX_TEXT_BYTES
        val slice = if (truncated) bytes.copyOf(MAX_TEXT_BYTES) else bytes
        // 二进制文件别硬当文本读（PC 也是这么判的：前 2048 字节里有 NUL 就是二进制）
        if (slice.take(2048).any { it == 0.toByte() }) {
            return Result.Unsupported(name, "这看着是二进制文件，读出来只会是乱码。")
        }
        val body = String(slice, Charsets.UTF_8)
        Result.Text(
            name,
            body + if (truncated) "\n…（文件较大，只读了前 ${MAX_TEXT_BYTES / 1024}KB）" else "",
        )
    }.getOrElse { Result.Unsupported(name, "读这个文件失败：${it.message}") }

    /** docx/pptx/xlsx 都是 zip：抽出里面的 XML，剥掉标签就是文字。 */
    private fun readZipDoc(context: Context, uri: Uri, name: String, ext: String): Result = runCatching {
        val wanted = when (ext) {
            "docx" -> listOf("word/document.xml")
            "pptx" -> null // 幻灯片分散在 ppt/slides/slideN.xml，全收
            else -> listOf("xl/sharedStrings.xml")
        }
        val builder = StringBuilder()
        context.contentResolver.openInputStream(uri)?.use { input ->
            ZipInputStream(input).use { zip ->
                var entry = zip.nextEntry
                while (entry != null) {
                    val entryName = entry.name
                    val hit = when {
                        wanted != null -> entryName in wanted
                        else -> entryName.startsWith("ppt/slides/slide") && entryName.endsWith(".xml")
                    }
                    if (hit) {
                        val xml = zip.readBytes().toString(Charsets.UTF_8)
                        // 段落/单元格之间补换行，否则整篇会挤成一行
                        val text = xml
                            .replace("</w:p>", "\n")
                            .replace("</a:p>", "\n")
                            .replace("</si>", "\n")
                            .replace(Regex("<[^>]+>"), "")
                            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                            .replace("&quot;", "\"").replace("&apos;", "'")
                        if (text.isNotBlank()) builder.append(text.trim()).append("\n")
                        if (builder.length > MAX_TEXT_BYTES) break
                    }
                    zip.closeEntry()
                    entry = zip.nextEntry
                }
            }
        }
        val body = builder.toString().trim()
        if (body.isEmpty()) {
            Result.Unsupported(name, "这个 ${ext.uppercase()} 里没抽出文字（可能是纯图片的文档）。")
        } else {
            Result.Text(
                name,
                body.take(MAX_TEXT_BYTES) +
                    if (body.length > MAX_TEXT_BYTES) "\n…（只读了前 ${MAX_TEXT_BYTES / 1024}KB）" else "",
            )
        }
    }.getOrElse { Result.Unsupported(name, "解析这个 ${ext.uppercase()} 失败：${it.message}") }

    /**
     * PDF：渲染成图片（视觉模型能直接看版式、表格、公式，比抽文字更忠实）。
     *
     * 页数上限 [MAX_PDF_PAGES]：每页一张图，多了又慢又贵。
     */
    private fun readPdf(context: Context, uri: Uri, name: String): Result = runCatching {
        val pfd: ParcelFileDescriptor = context.contentResolver.openFileDescriptor(uri, "r")
            ?: return Result.Unsupported(name, "打不开这个 PDF。")
        pfd.use { descriptor ->
            PdfRenderer(descriptor).use { renderer ->
                val pages = minOf(renderer.pageCount, MAX_PDF_PAGES)
                if (pages == 0) return Result.Unsupported(name, "这个 PDF 没有页面。")
                val urls = (0 until pages).mapNotNull { index ->
                    renderer.openPage(index).use { page ->
                        // 按 A4 比例、长边约 1568（与拍照那条路一致，省流量也够看清）
                        val scale = 1568f / maxOf(page.width, page.height)
                        val w = (page.width * scale).toInt().coerceAtLeast(1)
                        val h = (page.height * scale).toInt().coerceAtLeast(1)
                        val bitmap = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
                        // PDF 页面本身透明，先铺白底，否则渲染出来是黑的
                        bitmap.eraseColor(Color.WHITE)
                        page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                        val bytes = ByteArrayOutputStream().use { out ->
                            bitmap.compress(Bitmap.CompressFormat.JPEG, 85, out)
                            out.toByteArray()
                        }
                        bitmap.recycle()
                        "data:image/jpeg;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
                    }
                }
                val note = if (renderer.pageCount > pages) {
                    "（共 ${renderer.pageCount} 页，只看了前 $pages 页）"
                } else ""
                Result.Images(name + note, urls)
            }
        }
    }.getOrElse { Result.Unsupported(name, "渲染这个 PDF 失败：${it.message}") }

    /** 附件内容拼进消息文本时的形状（与 PC 的"文件："段落同义）。 */
    fun renderText(result: Result.Text): String =
        "【附件：${result.name}】\n${result.body}\n【附件结束】"
}
