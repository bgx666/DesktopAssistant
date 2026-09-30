package com.xiaozhu.planner.core.memory

/**
 * 检索到的一张图片的**索引信息**（不含图片本体）。
 *
 * 放在 memory 包而不是 tools 包：这是"记忆里的一张图"这个领域概念，
 * `MemoryStore.findImages` 返回它、`list_images` 工具渲染它、`show_image` 靠它取图——
 * 让 tools 依赖 memory 是对的，反过来（memory 依赖 tools）就绕了。
 */
data class ImageHit(
    /** 稳定 id，形如 `msg:42:0`（buffer 里第 42 条的第 0 张）或 `node:node1_003:1`。 */
    val id: String,
    /** 发送/拍摄时间，`yyyy-MM-dd HH:mm:ss`。 */
    val ts: String,
    /** 发图时用户说的话（检索线索；只发图没说话就是空）。 */
    val said: String = "",
    /** 所属记忆节点的摘要（图已经被压进记忆树时才有）。 */
    val nodeSummary: String = "",
    /** `sha1:…` 引用或老格式 base64 —— show_image 靠它取图。 */
    val ref: String = "",
)
