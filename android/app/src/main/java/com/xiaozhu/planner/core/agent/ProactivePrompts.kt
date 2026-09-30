package com.xiaozhu.planner.core.agent

/**
 * 系统主动触发的那些轮次，喂给模型的第一句话长什么样。
 *
 * 单独抽出来的原因有两个：
 * 1. 这些话**必须与 PC 版同义**（prompt 里已经写明"以 `（系统：` 开头的不是用户消息"），
 *    而它们现在有**两个出处**——界面在前台时由 ViewModel 拼，后台服务自己跑时由服务拼。
 *    两处各写一遍，迟早会漂。
 * 2. 纯函数，可以直接单测措辞（比如"别让模型以为这是用户在说话"）。
 */
object ProactivePrompts {

    /** 日历到点提醒。 */
    fun reminder(hhmm: String, lines: List<String>): String =
        "（系统：现在 $hhmm，日历上到点的是：${lines.joinToString("；")}。" +
            "这是用户自己排的安排——你判断该提醒他、还是该先自己处理、" +
            "或者其实已经做过了（那就顺势问一句）。）"

    /**
     * 心跳到点（"我该自己醒一次了"）。
     *
     * [awayFor] 是"距用户上次说话多久"——prompt 明确要求用时间信息判断用户状态，
     * 所以这句必须带上，否则模型只能瞎猜该不该说话。
     */
    fun heartbeat(hhmm: String, note: String, awayFor: String? = null): String = buildString {
        append("（系统：现在 ").append(hhmm).append("，你设的心跳到点了")
        if (!awayFor.isNullOrBlank()) append("；用户上次说话是 ").append(awayFor).append("前")
        append("。")
        if (note.isNotBlank()) append("醒来时要做的：").append(note).append("。")
        append("你可以：1) 跟用户说点什么（有事、或确实想聊）；")
        append("2) 自主学习：用 web_search / fetch_web / explore_memory_tree ")
        append("去了解你好奇的东西，把学到的写下来，它会进入你的长期记忆（自主学习时不要朗读）；")
        append("3) 什么都不做，只是待命。")
        append("做完后照常用 heartbeat 定下一次什么时候醒——间隔由你自己判断。）")
    }

    /** 补提醒：关机/被杀期间到点的安排（Android 特有的一条路径）。 */
    fun missedReminder(hhmm: String, lines: List<String>): String =
        "（系统：现在 $hhmm。刚才我不在（手机重启或系统把我停了），" +
            "这期间到点的是：${lines.joinToString("；")}。" +
            "先说一声，别让它悄悄沉底——你判断现在提醒还来不来得及、要不要改期。））"
}
