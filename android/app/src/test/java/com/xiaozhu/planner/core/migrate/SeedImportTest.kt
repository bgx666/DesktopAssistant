package com.xiaozhu.planner.core.migrate

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.data.db.CalendarItemEntity
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 「装完自动导入」的判定与执行。
 *
 * 三条护栏都必须成立，否则后果都不是小事：
 * - 库非空还导入 → 用户自己刚聊出来的内容被旧数据灌一遍（重复记忆）
 * - 导过还导入 → 用户把记忆清空后"复活"旧对话，像闹鬼
 * - 包里没 seed 却尝试 → 每次启动白读一次 6.5MB
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class SeedImportTest {

    private lateinit var db: PlannerDatabase
    private lateinit var context: Context

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        db = Room.inMemoryDatabaseBuilder(context, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
    }

    @After
    fun tearDown() = db.close()

    @Test
    fun `判定：三个条件缺一不可`() {
        assertTrue(SeedImport.shouldImport(alreadyImported = false, hasLocalData = false, hasSeedAsset = true))
        assertFalse("导过就不该再导", SeedImport.shouldImport(true, false, true))
        assertFalse("库非空就不该覆盖", SeedImport.shouldImport(false, true, true))
        assertFalse("包里没 seed 就跳过", SeedImport.shouldImport(false, false, false))
    }

    @Test
    fun `库里已有数据时不导入`() = runBlocking {
        db.calendar().insert(CalendarItemEntity(date = "2026-10-05", content = "手机上自己加的"))

        val outcome = SeedImport.importIfNeeded(context, db)

        assertEquals(SeedImport.Outcome.Skipped, outcome)
        assertEquals("原有数据不能被覆盖", 1, db.calendar().rangeAll("2026-01-01", "2027-01-01").size)
    }

    @Test
    fun `第二次调用不再导入（标记生效）`() = runBlocking {
        // 第一次：有没有 seed 都行，结果只影响是否 Imported
        val first = SeedImport.importIfNeeded(context, db)
        val second = SeedImport.importIfNeeded(context, db)

        assertEquals("第二次必须跳过", SeedImport.Outcome.Skipped, second)
        assertTrue(
            "第一次要么导入要么跳过，不该失败：$first",
            first is SeedImport.Outcome.Imported || first === SeedImport.Outcome.Skipped,
        )
    }

    @Test
    fun `包里带了 seed 时确实导进来（本机构建才有这个文件）`() = runBlocking {
        val hasSeed = runCatching { context.assets.open(SeedImport.ASSET_PATH).use { true } }
            .getOrDefault(false)
        org.junit.Assume.assumeTrue("这个构建里没有 assets/seed，跳过", hasSeed)

        val outcome = SeedImport.importIfNeeded(context, db)

        assertTrue("有 seed 就该导入：$outcome", outcome is SeedImport.Outcome.Imported)
        val imported = outcome as SeedImport.Outcome.Imported
        assertTrue("节点数应为正", imported.nodes > 0)
        assertTrue("日历项应为正", imported.calendar > 0)
        assertEquals(imported.nodes, MemoryStore(db).nodeCount())
    }
}
