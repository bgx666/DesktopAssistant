package com.xiaozhu.planner.core.image

import com.xiaozhu.planner.core.llm.PlannerJson
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer

/**
 * 把库里**老格式的内嵌 base64 图片**搬到文件里，行里只留引用。
 *
 * 设计目标是"**不可能丢图**"，所以：
 *
 * 1. **先写文件、成功了才改那一行**。顺序反了才会出现"数据库指向不存在的文件"。
 * 2. **逐行独立**：某一行失败（磁盘满、写文件异常）就跳过它，那一行**原样留着 base64**，
 *    功能完全正常，下次启动继续重试。不做"整个库一起改"。
 * 3. **读取端永远同时认两种格式**（见 [MessageMapping.rawImagesOf]）——
 *    所以迁移做了一半、或者迁移代码本身有 bug，半迁移状态也是可用的。
 * 4. 每次启动只搬一小批，不阻塞任何操作；全部搬完就是一次空查询。
 */
object ImageMigration {

    /** 每次启动最多搬多少行（一行可能带好几张图）。 */
    private const val BATCH = 20

    /**
     * 推进一批迁移。
     *
     * @return 本次搬了多少行；0 表示已经没有需要搬的了
     */
    suspend fun runBatch(db: PlannerDatabase): Int {
        val rows = db.messages().withInlineImages(BATCH)
        if (rows.isEmpty()) return 0

        var migrated = 0
        rows.forEach { row ->
            val raw = row.images ?: return@forEach
            val values = runCatching {
                PlannerJson.decodeFromString(ListSerializer(String.serializer()), raw)
            }.getOrNull() ?: return@forEach

            val inline = values.filter { ImageStore.isInline(it) }
            if (inline.isEmpty()) return@forEach

            // 关键顺序：**先把每一张都写进文件**，全成功才更新这一行
            val converted = inline.map { value -> ImageStore.put(value) }
            if (converted.any { it == null }) return@forEach // 有一张没写成就整行不动，下次再来

            val newValues = values.map { value ->
                if (ImageStore.isInline(value)) {
                    converted[inline.indexOf(value)] ?: value
                } else {
                    value
                }
            }
            val encoded = PlannerJson.encodeToString(ListSerializer(String.serializer()), newValues)
            // 只改 images 这一列，别的字段一律不碰
            runCatching {
                db.messages().updateImages(row.seq, encoded)
            }.onSuccess { migrated++ }
        }
        return migrated
    }

    /** 还有多少行等着搬（设置页/图片页显示进度用）。 */
    suspend fun pendingCount(db: PlannerDatabase): Int = db.messages().countInlineImages()
}
