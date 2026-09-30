package com.xiaozhu.planner.core.calendar

/**
 * 「日程变了，去把闹钟重排一遍」的抽象。
 *
 * 工具层只依赖这个接口，实际实现用 AlarmManager（`reminder/ReminderScheduler`），
 * 单测里传个假实现就能验证"改完日程有没有要求重排"。
 *
 * 为什么是"整体重排"而不是"注册/注销单个闹钟"：一次性闹钟的增删很容易漏
 * （改期、标完成、取消系列……每条路径都要记得），而重排就是把未来所有待办项
 * 重新登记一遍——幂等、漏不掉，代价只是几十毫秒。
 */
interface ReminderPlanner {
    suspend fun sync()
}
