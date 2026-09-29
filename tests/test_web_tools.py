"""网页搜索工具测试：web_search / fetch_web（monkeypatch httpx，不真联网）。"""

import httpx as httpx_mod

import planner.tools as tools_mod
from planner.tools import build_tools

BING_HTML = """
<ol id="b_results">
  <li class="b_algo"><h2><a href="https://example.com/a">示例新闻标题 A</a></h2>
    <div class="b_caption"><p>这是第一条结果的摘要内容。</p></div></li>
  <li class="b_algo"><h2><a href="https://example.com/b">示例新闻标题 B</a></h2>
    <div class="b_caption"><p>第二条摘要。</p></div></li>
</ol>
"""


def test_parse_bing_results():
    items = tools_mod._parse_bing_results(BING_HTML, 5)
    assert len(items) == 2
    assert items[0]["title"] == "示例新闻标题 A"
    assert items[0]["url"] == "https://example.com/a"
    assert "第一条结果" in items[0]["snippet"]


def test_parse_bing_limits():
    assert len(tools_mod._parse_bing_results(BING_HTML, 1)) == 1
    assert tools_mod._parse_bing_results("<html>nothing</html>", 5) == []


def test_web_search_success(monkeypatch):
    """查询原样交给搜索引擎（不预处理、不加引号）。"""
    calls = {}

    class _FakeResp:
        text = BING_HTML

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        calls["url"] = url
        calls["params"] = kw.get("params")
        return _FakeResp()

    monkeypatch.setattr(httpx_mod, "get", fake_get)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["web_search"].invoke({"query": "四川大学 校历"})
    assert "示例新闻标题 A" in res
    assert "https://example.com/a" in res
    assert calls["params"]["q"] == "四川大学 校历", "查询应原样转发，不做任何加工"


def test_web_search_failure(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx_mod, "get", boom)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["web_search"].invoke({"query": "x"})
    assert "搜索失败" in res
    # 空关键词
    res2 = tools["web_search"].invoke({"query": "  "})
    assert "不能为空" in res2


def test_extract_links():
    html = """
    <a href="/xl.htm">校历</a>
    <a href="https://jwc.scu.edu.cn/xl.htm">本科校历</a>
    <a href="javascript:void(0)">忽略</a>
    <a href="#top">忽略2</a>
    <a href="https://jwc.scu.edu.cn/xl.htm">重复链接</a>
    <a href="/news/1.htm">一篇非常长的文章标题超过六十个字符应该被过滤掉因为太长了没有价值</a>
    <a href="https://zs.scu.edu.cn/">招生网</a>
    """
    links = tools_mod._extract_links(html, "https://jwc.scu.edu.cn/index.htm", limit=10)
    hrefs = [h for _, h in links]
    assert "https://jwc.scu.edu.cn/xl.htm" in hrefs          # 相对路径补全 + 绝对地址
    assert "https://zs.scu.edu.cn/" in hrefs
    assert "javascript" not in " ".join(hrefs)
    assert "javascript:void(0)" not in hrefs
    # 重复去重：xl.htm 只出现一次
    assert hrefs.count("https://jwc.scu.edu.cn/xl.htm") == 1


def test_fetch_web(monkeypatch):
    class _FakeResp:
        text = ("<html><script>var x=1;</script><style>a{}</style>"
                "<body><h1>标题</h1><p>正文内容段落。</p>"
                '<a href="/xl.htm">校历</a></body></html>')

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return _FakeResp()

    monkeypatch.setattr(httpx_mod, "get", fake_get)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["fetch_web"].invoke({"url": "https://example.com/page"})
    assert "标题" in res and "正文内容段落" in res
    assert "var x=1" not in res, "脚本内容应被剔除"
    assert "【页面链接】" in res, "应附页面链接列表"
    assert "校历" in res and "xl.htm" in res
    # 非 http 链接拒绝
    res2 = tools["fetch_web"].invoke({"url": "file:///etc/passwd"})
    assert "只支持" in res2
    # 截断
    long = _FakeResp()
    long.text = "<p>" + "字" * 5000 + "</p>"
    monkeypatch.setattr(httpx_mod, "get", lambda url, **kw: long)
    res3 = tools["fetch_web"].invoke({"url": "https://e.com", "max_chars": 1000})
    assert "截断" in res3 and len(res3) < 2000


def test_web_search_failure(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx_mod, "get", boom)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["web_search"].invoke({"query": "x"})
    assert "搜索失败" in res
    # 空关键词
    res2 = tools["web_search"].invoke({"query": "  "})
    assert "不能为空" in res2


def test_fetch_web(monkeypatch):
    class _FakeResp:
        text = ("<html><script>var x=1;</script><style>a{}</style>"
                "<body><h1>标题</h1><p>正文内容段落。</p>"
                '<a href="/xl.htm">校历</a></body></html>')

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return _FakeResp()

    monkeypatch.setattr(httpx_mod, "get", fake_get)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["fetch_web"].invoke({"url": "https://example.com/page"})
    assert "标题" in res and "正文内容段落" in res
    assert "var x=1" not in res, "脚本内容应被剔除"
    assert "【页面链接】" in res, "应附页面链接列表"
    assert "校历" in res and "xl.htm" in res
    # 非 http 链接拒绝
    res2 = tools["fetch_web"].invoke({"url": "file:///etc/passwd"})
    assert "只支持" in res2
    # 截断
    long = _FakeResp()
    long.text = "<p>" + "字" * 5000 + "</p>"
    monkeypatch.setattr(httpx_mod, "get", lambda url, **kw: long)
    res3 = tools["fetch_web"].invoke({"url": "https://e.com", "max_chars": 1000})
    assert "截断" in res3 and len(res3) < 2000


def test_fetch_web_failure(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(httpx_mod, "get", boom)
    tools = {t.name: t for t in build_tools(None)}
    assert "抓取失败" in tools["fetch_web"].invoke({"url": "https://e.com"})


# --------------------------------------------------------------- 360 主引擎
SO_HTML = """
<div class="result">
  <h3 class="res-title"><a href="https://www.so.com/link?m=AAA" data-mdurl="https://arxiv.org/html/2412.19437v1"
     data-res='{"tp":21}' target="_blank">DeepSeek-V3 <em>Technical</em> Report</a></h3>
  <p class="res-desc"><span class="g-c-gray">2025年1月 - </span>官方技术报告，<em>DeepSeek</em> 的完整细节。</p>
  <p class="g-linkinfo"><cite><a href="https://www.so.com/link?m=BBB">arxiv.org</a></cite></p>
</div>
<div class="result">
  <h3 class="res-title"><a href="https://www.so.com/link?m=CCC" data-mdurl="https://blog.csdn.net/x/y"
     target="_blank">论文精读 DeepSeek-V3</a></h3>
  <span class="res-list-summary">这是 <em>摘要</em> 内容。</span>
</div>
<h3 class="res-title"><a href="https://hao.360.com/?src=hj_so">360导航</a></h3>
"""


def test_parse_so_results():
    items = tools_mod._parse_so_results(SO_HTML, 5)
    assert len(items) == 2, "360 自家导航链接应被过滤掉"
    assert items[0]["title"] == "DeepSeek-V3 Technical Report", "<em> 高亮标签应被剥掉"
    assert items[0]["url"] == "https://arxiv.org/html/2412.19437v1", "应取 data-mdurl 真实地址"
    assert "官方技术报告" in items[0]["snippet"]
    assert "摘要" in items[1]["snippet"]


def test_parse_so_results_limits_and_garbage():
    assert len(tools_mod._parse_so_results(SO_HTML, 1)) == 1
    assert tools_mod._parse_so_results("<html>no results</html>", 5) == []


def test_web_search_prefers_so360(monkeypatch):
    """360 有结果时直接用，不再打必应。"""
    calls = []

    class _Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(SO_HTML if "so.com" in url else BING_HTML)

    monkeypatch.setattr(httpx_mod, "get", fake_get)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["web_search"].invoke({"query": "DeepSeek V3 技术报告"})
    assert "DeepSeek-V3 Technical Report" in res
    assert "来源 360" in res
    assert len(calls) == 1 and "so.com" in calls[0], "360 命中就不该再请求必应"


def test_web_search_falls_back_to_bing(monkeypatch):
    """360 解析不到结果时回退必应。"""
    calls = []

    class _Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp("<html>360 blocked</html>" if "so.com" in url else BING_HTML)

    monkeypatch.setattr(httpx_mod, "get", fake_get)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["web_search"].invoke({"query": "四川大学 校历"})
    assert "示例新闻标题 A" in res
    assert "来源 bing" in res
    assert any("so.com" in u for u in calls) and any("bing.com" in u for u in calls)


def test_web_search_distinguishes_empty_from_error(monkeypatch):
    """引擎通了但没结果 → 提示换关键词；引擎全报错 → 提示搜索失败。"""
    class _Resp:
        text = "<html>nothing</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx_mod, "get", lambda url, **kw: _Resp())
    tools = {t.name: t for t in build_tools(None)}
    assert "没有搜到" in tools["web_search"].invoke({"query": "asdfghjkl"})

    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx_mod, "get", boom)
    assert "搜索失败" in tools["web_search"].invoke({"query": "asdfghjkl"})
