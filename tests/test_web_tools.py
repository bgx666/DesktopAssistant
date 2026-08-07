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
    res = tools["web_search"].invoke({"query": "最新科技新闻"})
    assert "示例新闻标题 A" in res
    assert "https://example.com/a" in res
    assert calls["params"]["q"] == "最新科技新闻"


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
                "<body><h1>标题</h1><p>正文内容段落。</p></body></html>")

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return _FakeResp()

    monkeypatch.setattr(httpx_mod, "get", fake_get)
    tools = {t.name: t for t in build_tools(None)}
    res = tools["fetch_web"].invoke({"url": "https://example.com/page"})
    assert "标题" in res and "正文内容段落" in res
    assert "var x=1" not in res, "脚本内容应被剔除"
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
