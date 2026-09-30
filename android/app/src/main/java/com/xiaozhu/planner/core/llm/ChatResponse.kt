package com.xiaozhu.planner.core.llm

import kotlinx.serialization.Serializable

/** 非流式响应里我们需要的部分。 */
@Serializable
internal data class ChatResponse(val choices: List<ChatChoice> = emptyList())

@Serializable
internal data class ChatChoice(val message: ChatResponseMessage? = null)

@Serializable
internal data class ChatResponseMessage(val content: String? = null)