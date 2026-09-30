package com.xiaozhu.planner.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.RawQuery
import androidx.sqlite.db.SupportSQLiteQuery

@Dao
interface NodeDao {

    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insert(node: NodeEntity)

    @Query("SELECT * FROM nodes WHERE characterId = :cid AND id = :id")
    suspend fun byId(cid: String, id: String): NodeEntity?

    @Query(
        "SELECT * FROM nodes WHERE characterId = :cid AND level = :level AND isActive = 1 " +
            "ORDER BY roundStart, id"
    )
    suspend fun activeAtLevel(cid: String, level: Int): List<NodeEntity>

    @Query("SELECT COUNT(*) FROM nodes WHERE characterId = :cid AND level = :level AND isActive = 1")
    suspend fun activeCountAtLevel(cid: String, level: Int): Int

    /** 根 = 层级最高的节点里最早的那个。 */
    @Query("SELECT id FROM nodes WHERE characterId = :cid ORDER BY level DESC, roundStart LIMIT 1")
    suspend fun topNodeId(cid: String): String?

    @Query("SELECT * FROM nodes WHERE characterId = :cid AND parentId = :parentId ORDER BY roundStart, id")
    suspend fun childrenOf(cid: String, parentId: String): List<NodeEntity>

    /** 该层已用的最大序号（重启后 id 不重号）。 */
    @Query(
        "SELECT MAX(CAST(SUBSTR(id, INSTR(id, '_') + 1) AS INTEGER)) FROM nodes " +
            "WHERE characterId = :cid AND level = :level"
    )
    suspend fun maxSeqAtLevel(cid: String, level: Int): Int?

    @Query("UPDATE nodes SET parentId = :parentId, isActive = 0 WHERE characterId = :cid AND id IN (:childIds)")
    suspend fun attachToParent(cid: String, parentId: String, childIds: List<String>)

    @Query("SELECT COUNT(*) FROM nodes WHERE characterId = :cid")
    suspend fun count(cid: String): Int

    /** 导入旧数据时用：id 沿用导出里的编号，冲突就覆盖。 */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAllReplace(nodes: List<NodeEntity>)

    @Query("DELETE FROM nodes WHERE characterId = :cid")
    suspend fun clearAll(cid: String)

    @RawQuery
    suspend fun search(query: SupportSQLiteQuery): List<NodeEntity>
}

@Dao
interface MessageDao {

    @Insert
    suspend fun insert(message: MessageEntity): Long

    /** 导入旧对话时按顺序批量插入（seq 自增，顺序即时间顺序）。 */
    @Insert
    suspend fun insertAll(messages: List<MessageEntity>)

    @Query("SELECT * FROM messages ORDER BY seq")
    suspend fun all(): List<MessageEntity>

    /** 未压缩的原始消息（节点消息不算）。 */
    @Query("SELECT * FROM messages WHERE nodeId IS NULL ORDER BY seq")
    suspend fun raw(): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages WHERE nodeId IS NULL")
    suspend fun rawCount(): Int

    /** 最早的 n 条原始消息（要压缩的那批）。 */
    @Query("SELECT * FROM messages WHERE nodeId IS NULL ORDER BY seq LIMIT :n")
    suspend fun firstRaw(n: Int): List<MessageEntity>

    @Query("DELETE FROM messages WHERE seq IN (:seqs)")
    suspend fun deleteBySeq(seqs: List<Long>)

    @Query("SELECT * FROM messages WHERE nodeId IS NOT NULL ORDER BY nodeStart, seq")
    suspend fun nodeMessages(): List<MessageEntity>

    @Query("SELECT * FROM messages WHERE nodeId IN (:nodeIds)")
    suspend fun byNodeIds(nodeIds: List<String>): List<MessageEntity>

    @Query("DELETE FROM messages")
    suspend fun clear()
}

@Dao
interface BufferStateDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun save(state: BufferStateEntity)

    @Query("SELECT * FROM buffer_state WHERE characterId = :cid")
    suspend fun load(cid: String): BufferStateEntity?
}

/**
 * 日历项读写。时间比较一律用 `date || ' ' || COALESCE(time, '23:59')` 这种拼接字符串，
 * 与 PC 版 `calendar_db.py` 的判据保持一字不差（换实现时不容易悄悄改变语义）。
 */
@Dao
interface CalendarDao {

    @Insert
    suspend fun insert(item: CalendarItemEntity): Long

    @Insert
    suspend fun insertAll(items: List<CalendarItemEntity>)

    @Query("SELECT * FROM calendar_items WHERE id = :id")
    suspend fun byId(id: Long): CalendarItemEntity?

    /** 闭区间 [start, end]，全天项排当天最前（COALESCE 成 00:00）。 */
    @Query(
        "SELECT * FROM calendar_items WHERE date >= :start AND date <= :end " +
            "AND status != 'cancelled' ORDER BY date, COALESCE(time, '00:00'), id"
    )
    suspend fun range(start: String, end: String): List<CalendarItemEntity>

    /** 含已取消（日历页要能看到"取消了的痕迹"，工具列表则不看）。 */
    @Query(
        "SELECT * FROM calendar_items WHERE date >= :start AND date <= :end " +
            "ORDER BY date, COALESCE(time, '00:00'), id"
    )
    suspend fun rangeAll(start: String, end: String): List<CalendarItemEntity>

    @Query(
        "SELECT * FROM calendar_items WHERE date = :date AND status != 'cancelled' " +
            "ORDER BY COALESCE(time, '00:00'), id"
    )
    suspend fun day(date: String): List<CalendarItemEntity>

    // ── 白名单更新（Room 不支持动态 SET，逐字段写清楚）────────────

    @Query("UPDATE calendar_items SET date = :value WHERE id = :id")
    suspend fun setDate(id: Long, value: String): Int

    @Query("UPDATE calendar_items SET time = :value WHERE id = :id")
    suspend fun setTime(id: Long, value: String?): Int

    @Query("UPDATE calendar_items SET content = :value WHERE id = :id")
    suspend fun setContent(id: Long, value: String): Int

    @Query("UPDATE calendar_items SET detail = :value WHERE id = :id")
    suspend fun setDetail(id: Long, value: String): Int

    @Query("UPDATE calendar_items SET kind = :value WHERE id = :id")
    suspend fun setKind(id: Long, value: String): Int

    /**
     * 状态与 doneAt 必须一起改：标完成写时间、撤销完成清空，
     * 免得留下"已完成但没有完成时间"的脏数据（PC 版同一个约定）。
     */
    @Query("UPDATE calendar_items SET status = :status, doneAt = :doneAt WHERE id = :id")
    suspend fun setStatus(id: Long, status: String, doneAt: Long?): Int

    // ── 重复系列 ────────────────────────────────────────────

    @Query(
        "UPDATE calendar_items SET time = :time, kind = :kind, content = :content, detail = :detail " +
            "WHERE seriesId = :sid AND status = 'todo' AND date >= :fromDate"
    )
    suspend fun updateSeries(
        sid: String,
        fromDate: String,
        time: String?,
        kind: String,
        content: String,
        detail: String,
    ): Int

    @Query("UPDATE calendar_items SET status = 'cancelled', doneAt = NULL WHERE seriesId = :sid AND status = 'todo' AND date >= :fromDate")
    suspend fun cancelSeries(sid: String, fromDate: String): Int

    @Query(
        "SELECT seriesId AS seriesId, MIN(date) AS firstDate, MAX(date) AS lastDate, " +
            "MAX(`repeat`) AS repeat, MAX(repeatUntil) AS repeatUntil, MAX(content) AS content, " +
            "MAX(time) AS time, MAX(kind) AS kind, COUNT(*) AS total, " +
            "SUM(CASE WHEN status = 'todo' THEN 1 ELSE 0 END) AS todoCount " +
            "FROM calendar_items WHERE seriesId IS NOT NULL GROUP BY seriesId ORDER BY MIN(date)"
    )
    suspend fun seriesSummary(): List<SeriesRow>

    @Query(
        "SELECT * FROM calendar_items WHERE seriesId = :sid " +
            "ORDER BY date, COALESCE(time, '00:00'), id"
    )
    suspend fun seriesItems(sid: String): List<CalendarItemEntity>

    /** 需要续期的系列：已生成到的最后日期就在 cutoff 当天或更早。 */
    @Query(
        "SELECT seriesId FROM calendar_items WHERE seriesId IS NOT NULL AND status != 'cancelled' " +
            "GROUP BY seriesId HAVING MAX(COALESCE(repeatUntil, date)) <= :cutoff"
    )
    suspend fun seriesNeedExtend(cutoff: String): List<String>

    // ── 到点提醒 ────────────────────────────────────────────

    /** 到点且尚未提醒过的待办项（全天项不参与到点提醒，由晨间议程覆盖）。 */
    @Query(
        "SELECT * FROM calendar_items WHERE status = 'todo' AND time IS NOT NULL " +
            "AND remindedAt IS NULL AND (date || ' ' || time) <= :stamp ORDER BY date, time"
    )
    suspend fun due(stamp: String): List<CalendarItemEntity>

    @Query("UPDATE calendar_items SET remindedAt = :at WHERE id = :id")
    suspend fun markReminded(id: Long, at: String): Int

    /** 改期/改时刻后清掉提醒标记，否则改到未来就不再提醒了。 */
    @Query("UPDATE calendar_items SET remindedAt = NULL WHERE id = :id")
    suspend fun clearReminded(id: Long): Int

    /** 还没提醒过、且时刻/日期已经过去的 todo 项（跨重启补提醒用）。 */
    @Query(
        "SELECT * FROM calendar_items WHERE status = 'todo' AND remindedAt IS NULL " +
            "AND (date || COALESCE(' ' || time, ' 23:59')) < :stamp ORDER BY date, time"
    )
    suspend fun overdue(stamp: String): List<CalendarItemEntity>

    /** 未来的待办项（重排闹钟用）：还没提醒过、日期在今天或之后。 */
    @Query(
        "SELECT * FROM calendar_items WHERE status = 'todo' AND remindedAt IS NULL " +
            "AND date >= :today ORDER BY date, COALESCE(time, '00:00'), id"
    )
    suspend fun upcoming(today: String): List<CalendarItemEntity>

    /** 全部（工具/议程不再需要，但日历页要看历史）。 */
    @Query("SELECT * FROM calendar_items ORDER BY date DESC, COALESCE(time, '00:00') DESC LIMIT :limit")
    suspend fun recent(limit: Int): List<CalendarItemEntity>

    /** 导入旧数据时清空（重复系列靠 seriesId 串，id 不沿用）。 */
    @Query("DELETE FROM calendar_items")
    suspend fun clear()
}

/** 系列汇总（GROUP BY 的投影，Room 需要一个具名类型）。 */
data class SeriesRow(
    val seriesId: String,
    val firstDate: String,
    val lastDate: String,
    val repeat: String?,
    val repeatUntil: String?,
    val content: String,
    val time: String?,
    val kind: String,
    val total: Int,
    val todoCount: Int,
)
