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

    @RawQuery
    suspend fun search(query: SupportSQLiteQuery): List<NodeEntity>
}

@Dao
interface MessageDao {

    @Insert
    suspend fun insert(message: MessageEntity): Long

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