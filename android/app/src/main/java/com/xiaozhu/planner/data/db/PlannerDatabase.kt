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
    version = 2,
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

        @Volatile
        private var instance: PlannerDatabase? = null

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
                ).addMigrations(MIGRATION_1_2).build().also { instance = it }
            }
    }
}