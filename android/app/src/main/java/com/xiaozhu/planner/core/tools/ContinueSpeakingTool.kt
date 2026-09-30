package com.xiaozhu.planner.core.tools

import com.xiaozhu.planner.core.agent.SpeechPacing
import com.xiaozhu.planner.core.agent.Tool
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * `continue_speaking`：需要分点描述时，说一点 → 停一下 → 再说下一点。
 *
 * 它不是"继续干活"的手段（那是同一轮里接着调工具），而是**说话节奏**：
 * 回复会被朗读出来，一口气刷一大段听着像客服念稿。暂停时长按上一段字数算
 * （见 [SpeechPacing]，公式与 PC 版一致）。
 *
 * 暂停用 `delay`：用户点"停"时协程被取消，暂停立刻结束——等价于 PC 版的
 * "用户打断就立即唤醒"。
 */
class ContinueSpeakingTool(
    private val pacing: SpeechPacing = SpeechPacing(),
    private val sleep: suspend (Long) -> Unit = { delay(it) },
) : Tool {

    override val name = "continue_speaking"

    override val description =
        "需要分点、分段描述时调用：每调用一次会停顿片刻，然后你继续说下一点。" +
            "每说一点之前调用一次；所有点都说完了就不要再调用，正常收尾（如调用 heartbeat）。" +
            "不要为了一句普通的话调用它。"

    override val parameters: JsonObject = buildJsonObject {
        put("type", "object")
        putJsonObject("properties") {
            put("description", "不需要参数：调用它就表示「我还要接着说下一点」。")
        }
    }

    override suspend fun run(args: JsonObject): String {
        sleep((pacing.pauseSeconds * 1000).toLong())
        return "（收到，继续。请接着说下一点。）"
    }
}
