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
     * 客户端自报的 User-Agent。
     *
     * opencode 的 Go 网关**明确要求**客户端用自己的名字（`my-coding-agent/1.0` 这种），
     * 而不是通用的库名——默认的 `okhttp/4.12.0` 正属于它点名的那类。
     * 它们靠这个做流量监控（识别滥用），所以这不算可选的美化。
     */
    fun userAgent(versionName: String): String = "xiaozhu-android/$versionName"

    /**
     * 该端点需要的附加请求头。
     *
     * opencode 的 zen 网关没有 session 头会直接 400（`MissingSessionID`）。
     *
     * **无条件发**，不再只按域名判断：一开始写成"只有 host 含 opencode.ai 才发"，
     * 结果一旦 base 写法/域名与预期不同（换域名、走代理、用户手填），头就**静默不发**，
     * 表现成"改了半天还是 400"。而多一个 `x-` 开头的自定义头，对 OpenAI / DeepSeek
     * 这类服务商是完全无害的（它们按 RFC 忽略不认识的头）。
     */
    fun extraHeaders(base: String, sessionId: String?): Map<String, String> {
        val id = sessionId?.takeIf { it.isNotBlank() } ?: return emptyMap()
        return mapOf(SESSION_HEADER to id)
    }

    /**
     * 把服务商的报错翻译成"下一步该干什么"。
     *
     * 这一层踩过的坑都是**报错本身看不出病因**的：
     * 404 返回的是一整页 HTML（看着像服务挂了）、400 只说缺个头（不说客户端该发什么）。
     * 把这两种情况写成一句人话，用户就不用每次都来问。
     */
    fun hintFor(status: Int, body: String): String? = when {
        body.contains("MissingSessionID", ignoreCase = true) ->
            "网关要求 $SESSION_HEADER 头。去设置页点「测试连接」，那里会显示实际发出的头" +
                "和 session 值；如果 session 是空的，把那个框清空再保存会自动生成一个。"

        // 网关对"没有 messages"的回复只有一句 {"model":"..."}，完全看不出病因
        status == 400 && looksLikeModelOnlyError(body) ->
            "网关说这次请求里没有可用的 messages（它只回了模型名）。" +
                "多半是请求体不完整或消息列表为空；设置页「测试连接」里的「本次请求」会显示实际条数。"

        status == 404 && body.contains("<html", ignoreCase = true) ->
            "返回的是一张网页而不是接口响应 —— Base URL 多半写错了。" +
                "填到 /v1 为止即可（带 /chat/completions 也行，会自动去掉重复的一段）。"

        status == 401 || status == 403 ->
            "API Key 不对或没有权限（换服务商时 key 不通用）。"

        // DeepSeek 官方只认两个模型名；传 opencode 那套名字会 400，而且报错里
        // 直接列出了可用名字——把它翻成人话，省得再去查文档
        body.contains("supported API model names", ignoreCase = true) ->
            "这个端点不认这个模型名。DeepSeek 官方只支持 `deepseek-flash`" +
                "（它的正式名字是 DeepSeek-V4.1-Flash）与 `deepseek-v4-pro`；" +
                "`deepseek-v4.1-flash` 是 opencode 网关的名字，官方端点不认。"

        status == 429 -> "触发了限流，等一会儿再试。"

        status >= 500 -> "服务端出错，不是配置问题，稍后再试。"

        else -> null
    }

    /**
     * 是不是"只有模型名"的那种报错体（如 `{"model":"deepseek-v4.1-flash"}`）。
     *
     * **刻意不用正则**：原来写的是
     * `Regex("""\{\s*"model"\s*:\s*"[^"]*"\s*}""")`，里面有未配对的 `}`——
     * 桌面 JVM 的正则容忍它（所以单测全绿），而**安卓用的是 ICU 正则，
     * 会直接抛 PatternSyntaxException**。表现就是"电脑上测好的代码在真机上崩"。
     * 这种小判断用字符串操作更省事，也不会因为正则方言差异翻车。
     */
    private fun looksLikeModelOnlyError(body: String): Boolean {
        val t = body.trim()
        return t.length < 200 &&
            t.startsWith("{") && t.endsWith("}") &&
            t.contains("\"model\"") &&
            !t.contains("messages") &&
            !t.contains("error")
    }
}
