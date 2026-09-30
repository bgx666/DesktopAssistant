package com.xiaozhu.planner.data.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [
        NodeEntity::class,
        MessageEntity::class,
        BufferStateEntity::class,
        CalendarItemEntity::class,
    ],
    version = 3,
    exportSchema = false,
)
abstract class PlannerDatabase : RoomDatabase() {
    abstract fun nodes(): NodeDao
    abstract fun messages(): MessageDao
    abstract fun buffer(): BufferStateDao
    abstract fun calendar(): CalendarDao

    companion object {
        /**
         * v1 → v2：加日历表。
         *
         * 用真迁移而不是 `fallbackToDestructiveMigration`——v1 已经装进过测试机，
         * 破坏性迁移会把用户的记忆树和对话一起清掉，那是不可接受的代价。
         */
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS `calendar_items` (
                        `id` INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        `date` TEXT NOT NULL,
                        `time` TEXT,
                        `kind` TEXT NOT NULL,
                        `content` TEXT NOT NULL,
                        `detail` TEXT NOT NULL,
                        `status` TEXT NOT NULL,
                        `remindedAt` TEXT,
                        `createdAt` INTEGER NOT NULL,
                        `doneAt` INTEGER,
                        `seriesId` TEXT,
                        `repeat` TEXT,
                        `repeatUntil` TEXT
                    )
                    """.trimIndent()
                )
                db.execSQL("CREATE INDEX IF NOT EXISTS `index_calendar_items_date_time` ON `calendar_items` (`date`, `time`)")
                db.execSQL("CREATE INDEX IF NOT EXISTS `index_calendar_items_seriesId` ON `calendar_items` (`seriesId`)")
                db.execSQL("CREATE INDEX IF NOT EXISTS `index_calendar_items_status_remindedAt` ON `calendar_items` (`status`, `remindedAt`)")
            }
        }

        /**
         * v2 → v3：消息加 `images` 列（图片输入）。
         *
         * 只加一列、老行填 NULL，所以老数据一条都不会动。
         */
        val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE `messages` ADD COLUMN `images` TEXT")
            }
        }

        @Volatile
        private var instance: PlannerDatabase? = null

        /**
         * 全部迁移，**按顺序排好**（声明在最后：数组引用上面的常量，
         * Kotlin 的伴生对象按书写顺序初始化）。
         *
         * 加一条的规矩：改了实体（加字段/加表）就 `version + 1`，并在这里补一条
         * `Migration(n, n+1)`——**绝不允许用 `fallbackToDestructiveMigration`**，
         * 那会把用户的记忆树、对话、日历一起清掉。
         *
         * `PlannerDatabaseMigrationTest` 会机械校验"版本号与迁移链是否对得上"、
         * 以及"迁移跑完之后老数据还在不在"，所以忘了写迁移会在单测阶段就红。
         */
        val MIGRATIONS: Array<Migration> = arrayOf(
            MIGRATION_1_2,
            MIGRATION_2_3,
        )

        /**
         * 单例：Room 实例自带连接池，重复 build 会开出多个连接池同时写同一个文件
         * （WAL 下不报错，但会浪费内存、并可能出现读到旧快照的错觉）。
         */
        fun get(context: Context, name: String = "planner.db"): PlannerDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    PlannerDatabase::class.java,
                    name,
                ).addMigrations(*MIGRATIONS).build().also { instance = it }
            }
    }
}