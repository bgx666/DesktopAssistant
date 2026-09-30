package com.xiaozhu.planner.core.llm

/**
 * 端点 URL 的归一化。
 *
 * 为什么要有它：设置页里那个框叫「Base URL」，但用户（和文档、和各家控制台）
 * 经常直接把**完整端点**粘进来——`https://opencode.ai/zen/go/v1/chat/completions`。
 * 再拼一次就变成 `.../chat/completions/chat/completions`，而这类网关对未知路径
 * 往往返回**网站的 404 页面**（HTML），报错看起来像"服务挂了"，实际是路径重复。
 *
 * 所以这里两种写法都接受：带不带 `/chat/completions` 都行，多余的斜杠也吃掉。
 */
object EndpointUrl {

    private const val SUFFIX = "/chat/completions"

    /** 归一化成"根"形式（永远不带 `/chat/completions`，不带尾部斜杠）。 */
    fun normalize(base: String): String {
        var url = base.trim()
        // 粘进来的可能是完整的 chat/completions（可能还带查询串）
        url = url.substringBefore('?').substringBefore('#')
        while (url.endsWith("/")) url = url.dropLast(1)
        if (url.endsWith(SUFFIX)) url = url.dropLast(SUFFIX.length)
        while (url.endsWith("/")) url = url.dropLast(1)
        return url
    }

    /** 真正要 POST 的地址。 */
    fun chatCompletions(base: String): String = normalize(base) + SUFFIX

    /** 是不是 opencode 的网关（它额外要求一个 session 头，见 [sessionHeader]）。 */
    fun isOpenCode(base: String): Boolean = normalize(base).contains("opencode.ai", ignoreCase = true)

    /** opencode 网关要求的会话头名。 */
    const val SESSION_HEADER = "x-opencode-session"

    /**
     * 该端点需要的附加请求头。
     *
     * opencode 的 zen 网关没有 session 头会直接 400（`MissingSessionID`）；
     * 别的服务商多一个头也无害，所以只在确实是它的时候加。
     */
    fun extraHeaders(base: String, sessionId: String?): Map<String, String> =
        if (isOpenCode(base) && !sessionId.isNullOrBlank()) mapOf(SESSION_HEADER to sessionId)
        else emptyMap()
}
