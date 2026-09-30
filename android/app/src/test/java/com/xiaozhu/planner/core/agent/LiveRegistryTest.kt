package com.xiaozhu.planner.core.agent

import android.app.Application
import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.xiaozhu.planner.core.calendar.CalendarStore
import com.xiaozhu.planner.core.calendar.DndSettings
import com.xiaozhu.planner.core.calendar.DndStore
import com.xiaozhu.planner.core.calendar.ReminderPlanner
import com.xiaozhu.planner.core.llm.ChatMessage
import com.xiaozhu.planner.core.llm.EndpointUrl
import com.xiaozhu.planner.core.llm.LlmEvent
import com.xiaozhu.planner.core.llm.OpenAiClient
import com.xiaozhu.planner.core.memory.MemoryStore
import com.xiaozhu.planner.core.tools.HeartbeatStore
import com.xiaozhu.planner.data.db.PlannerDatabase
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * 拿**真实注册表里的全部工具**打真实网关。
 *
 * 为什么单独有这一个：`LiveGatewayTest` 里的工具是手写的单个简单 schema，
 * 只能证明"形状对"，证明不了"**这 11 个真实 schema 都能被网关接受**"。
 * 而真实故障正是后者——`continue_speaking` 的 `properties` 写成了字符串，
 * 一个工具非法 → 网关拒掉整个请求（只回一句模型名）。
 *
 * 注意它**不搭 ConversationEngine**：那要读加密设置，而 Robolectric 没有
 * AndroidKeyStore（直接 KeyStoreException，最该被验的东西反而验不了）。
 * 工具清单从 [ToolRegistryFactory] 取，与线上是同一份。
 *
 * 默认跳过；设 `PLANNER_LIVE_KEY` 才联网（见 LiveGatewayTest 的说明）。
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class LiveRegistryTest {

    private val key = System.getenv("PLANNER_LIVE_KEY").orEmpty()
    private val base = System.getenv("PLANNER_LIVE_BASE").orEmpty()
        .ifBlank { "https://opencode.ai/zen/go/v1" }
    private val model = System.getenv("PLANNER_LIVE_MODEL").orEmpty()
        .ifBlank { "deepseek-v4.1-flash" }

    private lateinit var db: PlannerDatabase
    private lateinit var registry: ToolRegistry

    private class FakeReminder : ReminderPlanner {
        override suspend fun sync() = Unit
    }

    private class FakeHeartbeat : HeartbeatStore {
        override fun setNextWake(atMillis: Long, note: String) = Unit
        override fun current(): Pair<Long, String>? = null
    }

    private class FakeDnd : DndStore {
        override fun load(): DndSettings = DndSettings()
        override fun save(settings: DndSettings) = Unit
    }

    @Before
    fun setUp() {
        val app = ApplicationProvider.getApplicationContext<Application>()
        db = Room.inMemoryDatabaseBuilder(app as Context, PlannerDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        registry = ToolRegistryFactory.create(
            store = MemoryStore(db),
            calendar = CalendarStore(db.calendar()),
            reminderPlanner = FakeReminder(),
            heartbeatStore = FakeHeartbeat(),
            heartbeatPlanner = null,
            dndStore = FakeDnd(),
        )
    }

    @After
    fun tearDown() = db.close()

    @Test
    fun `11 个真实工具 schema 全部合法（本地体检，不联网）`() {
        val specs = registry.specs()
        assertEquals("工具数量变了要一起看 prompt 的 A 类清单", 11, specs.size)
        val problems = ToolSchemaCheck.problems(specs)
        assertTrue(
            "schema 不合法会被网关拒掉整个请求：\n" + problems.joinToString("\n"),
            problems.isEmpty(),
        )
    }

    @Test
    fun `带上全部真实工具的一轮能被网关接受`() = runBlocking {
        assumeTrue("没设 PLANNER_LIVE_KEY，跳过联网检查", key.isNotBlank())

        val specs = registry.specs()
        val client = OpenAiClient(
            baseUrl = base,
            apiKey = key,
            model = model,
            extraHeaders = EndpointUrl.extraHeaders(base, "live-registry-session"),
            userAgent = EndpointUrl.userAgent("live-check"),
        )
        val events = withTimeout(90_000) {
            client.stream(
                listOf(
                    ChatMessage.system("你是小助，用中文回答。"),
                    ChatMessage.user("不用查任何东西，直接回两个字：收到"),
                ),
                specs,
            ).toList()
        }

        val failed = events.filterIsInstance<LlmEvent.Failed>().firstOrNull()
        println("[live-registry] 工具数=${specs.size} 事件=${events.map { it::class.simpleName }}")
        assertTrue("带真实工具集的请求被拒：\n${failed?.message}", failed == null)
    }
}
