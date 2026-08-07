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
