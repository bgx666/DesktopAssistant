"""render_page 工具测试：无头渲染网页截图（monkeypatch 浏览器探测与 subprocess，不真跑浏览器）。

覆盖：视觉注入（data URL 进 _pending_screenshots）、http 直传、非视觉回退、找不到浏览器、
本地文件校验（不存在 / 黑名单）、渲染无产出时的报错。
"""

import base64
import subprocess
import types

import pytest

import planner.tools as tools_mod
from planner.tools import build_tools

# 1x1 透明 PNG（真实字节；fake 浏览器"截图"写这个）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _session(tmp_path, vision=True):
    return types.SimpleNamespace(
        vision_capable=vision,
        _pending_screenshots=[],
        data_root=tmp_path,
        settings={},
    )


def _fake_browser(monkeypatch, record, write=True):
    """假浏览器：解析 --screenshot= 参数并写出 1x1 PNG（不真启动进程）。"""
    monkeypatch.setattr(tools_mod, "_find_browser", lambda: "fake-browser")

    def fake_run(cmd, **kw):
        record["cmd"] = cmd
        out = [a for a in cmd if a.startswith("--screenshot=")][0].split("=", 1)[1]
        if write:
            with open(out, "wb") as f:
                f.write(PNG_1PX)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_render_page_vision_inject(monkeypatch, tmp_path):
    """本地文件 → file:// 渲染 → 截图注入上下文（与 view_image 同链路）。"""
    record = {}
    _fake_browser(monkeypatch, record)
    session = _session(tmp_path)
    page = tmp_path / "page.html"
    page.write_text("<h1>hi</h1>", encoding="utf-8")
    tools = {t.name: t for t in build_tools(session)}
    res = tools["render_page"].invoke({"url": str(page), "width": 800, "height": 600})
    assert "已渲染并截图" in res
    assert len(session._pending_screenshots) == 1
    shot = session._pending_screenshots[0]
    assert shot["url"].startswith("data:image/png;base64,")
    assert "page.html" in shot["text"]
    cmd = record["cmd"]
    assert cmd[0] == "fake-browser"
    assert "--headless=new" in cmd
    assert "--window-size=800,600" in cmd
    assert cmd[-1].startswith("file:///") and "page.html" in cmd[-1]
    # 截图落盘在隔离 data_root 下
    assert (tmp_path / "screenshots").is_dir()
    assert any((tmp_path / "screenshots").glob("shot_*.png"))


def test_render_page_http_passthrough(monkeypatch, tmp_path):
    """http 地址原样交给浏览器（dev server 场景），不做本地路径解析。"""
    record = {}
    _fake_browser(monkeypatch, record)
    tools = {t.name: t for t in build_tools(_session(tmp_path))}
    res = tools["render_page"].invoke({"url": "http://localhost:5173/"})
    assert "已渲染并截图" in res
    assert record["cmd"][-1] == "http://localhost:5173/"


def test_render_page_non_vision_returns_path(monkeypatch, tmp_path):
    """非视觉模型：不注入图片，返回截图路径。"""
    record = {}
    _fake_browser(monkeypatch, record)
    session = _session(tmp_path, vision=False)
    tools = {t.name: t for t in build_tools(session)}
    res = tools["render_page"].invoke({"url": "https://example.com"})
    assert "保存到" in res
    assert session._pending_screenshots == []


def test_render_page_no_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_mod, "_find_browser", lambda: None)
    tools = {t.name: t for t in build_tools(_session(tmp_path))}
    assert "没找到" in tools["render_page"].invoke({"url": "https://example.com"})


def test_render_page_local_file_checks(monkeypatch, tmp_path):
    """本地文件不存在 / 相对路径无工作区 / 空 url 的报错。"""
    record = {}
    _fake_browser(monkeypatch, record)
    tools = {t.name: t for t in build_tools(_session(tmp_path))}
    assert "文件不存在" in tools["render_page"].invoke({"url": str(tmp_path / "nope.html")})
    assert "相对路径" in tools["render_page"].invoke({"url": "index.html"})
    assert "不能为空" in tools["render_page"].invoke({"url": "  "})


def test_render_page_blocked_path(monkeypatch, tmp_path):
    """黑名单路径（.env）直接拒绝，不渲染。"""
    record = {}
    _fake_browser(monkeypatch, record)
    env = tmp_path / ".env"
    env.write_text("SECRET=1", encoding="utf-8")
    tools = {t.name: t for t in build_tools(_session(tmp_path))}
    assert "已拒绝" in tools["render_page"].invoke({"url": str(env)})


def test_render_page_no_output(monkeypatch, tmp_path):
    """浏览器没产出截图（崩溃/参数不支持）→ 报错并带浏览器输出。"""
    record = {}
    _fake_browser(monkeypatch, record, write=False)

    def fake_run(cmd, **kw):
        record["cmd"] = cmd
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tools = {t.name: t for t in build_tools(_session(tmp_path))}
    res = tools["render_page"].invoke({"url": "https://example.com"})
    assert "没有产出截图" in res and "boom" in res
