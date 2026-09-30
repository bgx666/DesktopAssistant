package com.xiaozhu.planner.data.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(
    entities = [NodeEntity::class, MessageEntity::class, BufferStateEntity::class],
    version = 1,
    exportSchema = false,
)
abstract class PlannerDatabase : RoomDatabase() {
    abstract fun nodes(): NodeDao
    abstract fun messages(): MessageDao
    abstract fun buffer(): BufferStateDao

    companion object {
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
                ).build().also { instance = it }
            }
    }
}