package com.xiaozhu.planner.core.memory

/**
 * 压缩指令（移植自 PC 版 `middleware.py` 的 `_LEAF_INSTRUCTION` / `_PARENT_INSTRUCTION`）。
 *
 * 三条要点是踩坑换来的，别删：
 * 1. **没有信息就留空**，不要为了填满而编造；
 * 2. summary 只写事件与决定，**画像不进 summary**（画像单独四维度）；
 * 3. **工具结果要高度压缩**：原文已随节点保存，摘要不需要复述它。
 */
object CompressionPrompts {

    fun leaf(originalText: String): String = """
        请把以下内容压缩为结构化 JSON（只输出 JSON，不要输出其他文字）。
        **各字段按实际内容填写：没有相关信息的字段保持空（profile 各维度留空数组、future_notes 留空数组），不要为了填充而编造、猜测或重复已有内容。**
        字段说明：
        1. summary：内容摘要——只总结事件、决定、对话进程；不要包含用户画像信息。
           **工具结果要高度压缩**：待压缩内容里的「工具结果」往往是工具返回的原文（如搜索到的网页正文），
           summary 只需提炼其中对后续对话有实际价值的信息（关键结论、数据、决定、用户原话要点），
           一两句话概括「小助从中获得了什么」，**不要转述、复述或浓缩工具返回的原文细节**。
        2. profile：用户画像——从这段对话中提炼用户特征，没有相关信息则对应维度留空数组：
           preferences 喜好偏好 / personality 性格特点 / habits 习惯作息 / goals 目标动机
           可参考上下文里已有节点的画像：延续已有认知、补充新观察，不重复不矛盾
        3. future_notes：后续说明——查看待压缩区域**之后**的消息，仅当它们直接解释/澄清/修正/补全了
           本段内容时填写（注明对应本段的哪个点 + 后面怎么说）；无关的不填；本段之后没有消息则留空

        待压缩内容：
        $originalText
        请给出压缩结果：
    """.trimIndent()

    fun parent(n: Int, childSummaries: String): String = """
        请把以下 $n 个节点压缩为结构化 JSON（只输出 JSON，不要输出其他文字）。
        **各字段按实际内容填写：没有相关信息的字段保持空，不要为了填充而编造、猜测或重复已有内容。**
        字段说明：
        1. summary：对子节点摘要的提炼
        2. profile：对子节点画像的提炼上卷（同维度合并归纳，没有相关信息则对应维度留空数组）：
           preferences 喜好偏好 / personality 性格特点 / habits 习惯作息 / goals 目标动机
        3. future_notes：子节点的后续说明若已被父级摘要/画像吸收则不必重复；若有新的、未被吸收的澄清再填写

        节点信息：
        $childSummaries
        请给出压缩结果：
    """.trimIndent()
}