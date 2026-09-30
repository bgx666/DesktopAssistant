package com.xiaozhu.planner.data.db

import android.content.Context
import androidx.sqlite.db.SupportSQLiteDatabase
import androidx.sqlite.db.SupportSQLiteOpenHelper
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * **升级不能丢数据**——这是这个 App 唯一不能出错的地方：
 * 用户的记忆树、对话、日历都在一个 SQLite 文件里，而 Android 只在卸载/清除数据时才会
 * 动它。剩下唯一的风险是**改了表结构却没写迁移**（Room 会直接抛异常或走破坏性迁移）。
 *
 * 两个断言：
 * 1. **迁移链与版本号对得上**：`@Database(version = N)` 必须等于"从 1 一路迁到 N"
 *    ——加了字段忘了写迁移、写了迁移忘了注册，都会在这里红。
 * 2. **迁移跑完老数据还在**：在 v1 结构上塞进节点/对话/运行状态，跑完迁移逐条查回来。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class PlannerDatabaseMigrationTest {

    /**
     * Room 实际用的 schema 版本 —— 从**真实库**里读 `PRAGMA user_version`。
     *
     * 不走 `@Database` 注解反射：它的保留级别是 CLASS，运行时读不到（一开始就是这么 NPE 的）。
     */
    private fun declaredVersion(): Int {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val db = androidx.room.Room.databaseBuilder(context, PlannerDatabase::class.java, "version-probe.db")
            .addMigrations(*PlannerDatabase.MIGRATIONS)
            .allowMainThreadQueries()
            .build()
        return try {
            db.openHelper.writableDatabase.query("PRAGMA user_version").use { c ->
                if (c.moveToFirst()) c.getInt(0) else 0
            }
        } finally {
            db.close()
        }
    }

    @Test
    fun `迁移链必须能从 1 一路走到当前版本`() {
        val version = declaredVersion()
        val steps = PlannerDatabase.MIGRATIONS
            .map { it.startVersion to it.endVersion }
            .sortedBy { it.first }

        assertTrue("一个迁移都没有，但库的版本号是 $version？", steps.isNotEmpty())

        var expected = 1
        steps.forEach { (from, to) ->
            assertEquals("迁移链断了：期望从 $expected 开始，实际从 $from 开始", expected, from)
            assertEquals("迁移必须一步一步走（$from → $to 跨度不对）", from + 1, to)
            expected = to
        }
        assertEquals(
            "库的版本号是 $version，但迁移只走到 $expected——" +
                "改了实体就必须补一条 Migration 并加进 MIGRATIONS",
            version,
            expected,
        )
    }

    @Test
    fun `迁移不改变老表的数据`() {
        val helper = v1Database()
        val db = helper.writableDatabase

        // v1 时期的数据：一个记忆节点、一条对话、一行运行状态
        db.execSQL(
            "INSERT INTO nodes (characterId, id, level, summary, isActive, searchText, createdAt) " +
                "VALUES ('assistant', 'node0_001', 0, '八月的对话摘要', 1, '八月的对话摘要', 1759020000000)"
        )
        db.execSQL(
            "INSERT INTO messages (role, content, tsMillis) VALUES ('user', '在吗', 1759020000000)"
        )
        db.execSQL(
            "INSERT INTO buffer_state " +
                "(characterId, msgCounter, round, compressedTotal, lastActivityAt, nextHeartbeatAt, " +
                "heartbeatMinutes, heartbeatNote, lastPlayerMessageAt) " +
                "VALUES ('assistant', 42, 0, 5172, 1759200000, 1759203600, 120.0, '看看论文清单', 1759199000)"
        )

        // 跑真正的迁移
        PlannerDatabase.MIGRATIONS.sortedBy { it.startVersion }.forEach { it.migrate(db) }

        // 老数据必须原样在
        db.query("SELECT id, summary FROM nodes").use { c ->
            assertTrue("记忆节点丢了", c.moveToFirst())
            assertEquals("node0_001", c.getString(0))
            assertEquals("八月的对话摘要", c.getString(1))
        }
        db.query("SELECT content FROM messages").use { c ->
            assertTrue("对话丢了", c.moveToFirst())
            assertEquals("在吗", c.getString(0))
        }
        db.query("SELECT compressedTotal FROM buffer_state").use { c ->
            assertTrue("运行状态丢了", c.moveToFirst())
            assertEquals(5172, c.getInt(0))
        }

        // 新表也建好了（迁移真的做了事）
        db.query("SELECT COUNT(*) FROM calendar_items").use { c ->
            assertTrue(c.moveToFirst())
            assertEquals(0, c.getInt(0))
        }
        // v3 新加的列也在，且老行的值为 NULL（图片是后加的能力）
        db.query("SELECT images FROM messages").use { c ->
            assertTrue(c.moveToFirst())
            assertTrue("老消息没有图片，应该是 NULL", c.isNull(0))
        }
        helper.close()
    }

    @Test
    fun `加图片列的迁移不动老数据`() {
        val helper = v1Database()
        val db = helper.writableDatabase
        db.execSQL("INSERT INTO messages (role, content, tsMillis) VALUES ('user', '老对话', 1759020000000)")

        PlannerDatabase.MIGRATIONS.sortedBy { it.startVersion }.forEach { it.migrate(db) }

        db.query("SELECT content, images FROM messages").use { c ->
            assertTrue(c.moveToFirst())
            assertEquals("老对话", c.getString(0))
            assertTrue(c.isNull(1))
        }
        // 新列可写（图片输入要靠它）
        db.execSQL("UPDATE messages SET images = '[\"data:image/jpeg;base64,AAAA\"]'")
        db.query("SELECT images FROM messages").use { c ->
            assertTrue(c.moveToFirst())
            assertTrue(c.getString(0).contains("base64"))
        }
        helper.close()
    }

    @Test
    fun `迁移可以重复跑（IF NOT EXISTS）`() {
        val helper = v1Database()
        val db = helper.writableDatabase
        // 手工先建一次日历表，再跑迁移：不该因为"表已存在"而炸
        PlannerDatabase.MIGRATION_1_2.migrate(db)
        PlannerDatabase.MIGRATION_1_2.migrate(db)
        db.query("SELECT COUNT(*) FROM calendar_items").use { c ->
            assertTrue(c.moveToFirst())
        }
        helper.close()
    }

    /** 一个只有 v1 结构的库（不走 Room，所以不涉及 identity hash 校验）。 */
    private fun v1Database(): SupportSQLiteOpenHelper {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val config = SupportSQLiteOpenHelper.Configuration.builder(context)
            .name(null) // 内存库
            .callback(object : SupportSQLiteOpenHelper.Callback(1) {
                override fun onCreate(db: SupportSQLiteDatabase) {
                    db.execSQL(
                        """
                        CREATE TABLE IF NOT EXISTS `nodes` (
                            `characterId` TEXT NOT NULL, `id` TEXT NOT NULL, `level` INTEGER NOT NULL,
                            `summary` TEXT NOT NULL, `parentId` TEXT, `roundStart` INTEGER,
                            `roundEnd` INTEGER, `timeStart` TEXT, `timeEnd` TEXT, `sourceRef` TEXT,
                            `details` TEXT, `profile` TEXT, `meta` TEXT, `isActive` INTEGER NOT NULL,
                            `searchText` TEXT NOT NULL, `createdAt` INTEGER NOT NULL,
                            PRIMARY KEY(`characterId`, `id`)
                        )
                        """.trimIndent()
                    )
                    db.execSQL(
                        """
                        CREATE TABLE IF NOT EXISTS `messages` (
                            `seq` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, `role` TEXT NOT NULL,
                            `content` TEXT NOT NULL, `tsMillis` INTEGER NOT NULL, `nodeId` TEXT,
                            `nodeStart` INTEGER, `toolCalls` TEXT, `toolCallId` TEXT, `name` TEXT
                        )
                        """.trimIndent()
                    )
                    db.execSQL(
                        """
                        CREATE TABLE IF NOT EXISTS `buffer_state` (
                            `characterId` TEXT NOT NULL, `msgCounter` INTEGER NOT NULL,
                            `round` INTEGER NOT NULL, `compressedTotal` INTEGER NOT NULL,
                            `lastActivityAt` INTEGER NOT NULL, `nextHeartbeatAt` INTEGER NOT NULL,
                            `heartbeatMinutes` REAL NOT NULL, `heartbeatNote` TEXT NOT NULL,
                            `lastPlayerMessageAt` INTEGER NOT NULL, PRIMARY KEY(`characterId`)
                        )
                        """.trimIndent()
                    )
                }

                override fun onUpgrade(db: SupportSQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
            })
            .build()
        return FrameworkSQLiteOpenHelperFactory().create(config)
    }
}
