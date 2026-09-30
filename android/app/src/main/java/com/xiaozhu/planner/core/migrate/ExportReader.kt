package com.xiaozhu.planner.core.migrate

import com.xiaozhu.planner.core.llm.PlannerJson
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.util.zip.GZIPInputStream

/**
 * 读 PC 导出的文件（JSON，或 `--gzip` 出来的 .gz）。
 *
 * 为什么不看扩展名判断压缩：从 SAF 选文件拿到的 Uri 常常没有可信后缀
 * （`content://...` 后面什么都不带）。**看魔数**（`1f 8b`）最可靠。
 *
 * 读不动就抛带说明的异常，绝不返回半份数据——迁移只做一次，
 * 静默导入一半比直接失败更难收拾。
 */
object ExportReader {

    private const val GZIP_MAGIC_1 = 0x1f
    private const val GZIP_MAGIC_2 = 0x8b
    private const val MAX_BYTES = 64L * 1024 * 1024   // 64MB：比这大的导出文件不正常

    class BadFile(message: String) : Exception(message)

    fun read(raw: ByteArray): ExportPayload {
        if (raw.isEmpty()) throw BadFile("文件是空的。")
        if (raw.size > MAX_BYTES) throw BadFile("文件超过 ${MAX_BYTES / 1024 / 1024}MB，不像导出文件。")

        val bytes = if (isGzip(raw)) {
            runCatching { GZIPInputStream(ByteArrayInputStream(raw)).use { it.readBytes() } }
                .getOrElse { throw BadFile("这个文件说是 gzip，但解不开：${it.message}") }
        } else {
            raw
        }

        val payload = runCatching { PlannerJson.decodeFromString(ExportPayload.serializer(), String(bytes, Charsets.UTF_8)) }
            .getOrElse { throw BadFile("不是小助的导出文件（JSON 解析失败：${it.message}）") }

        validate(payload)
        return payload
    }

    fun read(stream: InputStream): ExportPayload = read(stream.readBytes())

    fun isGzip(raw: ByteArray): Boolean =
        raw.size >= 2 && (raw[0].toInt() and 0xFF) == GZIP_MAGIC_1 && (raw[1].toInt() and 0xFF) == GZIP_MAGIC_2

    private fun validate(payload: ExportPayload) {
        if (payload.format != ExportPayload.EXPECTED_FORMAT) {
            throw BadFile("这不是小助的导出文件（format=${payload.format.ifBlank { "空" }}）。")
        }
        if (payload.formatVersion > ExportPayload.SUPPORTED_VERSION) {
            throw BadFile(
                "导出文件的格式版本是 ${payload.formatVersion}，这个 App 只认到 " +
                    "${ExportPayload.SUPPORTED_VERSION}。先升级 App 再导入。"
            )
        }
    }
}
