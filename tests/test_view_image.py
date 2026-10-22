"""view_image：本地图片读入模型上下文（注入/去重/OCR 回退/审批与黑名单）。

区内自由 + 区外审批（与 read_file 同策略）；只支持本地图片（不做网络图）。
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

import pytest

from planner.session import PlannerSession

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def session(data_root):
    s = PlannerSession(data_root, mock=True)
    yield s
    s.close()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "plot.png").write_bytes(PNG_1PX)
    return ws


def _tools(session):
    from planner.tools import build_tools
    return {t.name: t for t in build_tools(session)}


def _force_vision(monkeypatch):
    monkeypatch.setattr(PlannerSession, "vision_capable", property(lambda self: True))


def _drain_injected(session):
    """跑一次注入中间件 → 返回注入的 user 消息列表（并清空队列）。"""
    from planner.middleware import ScreenShotInjectMiddleware
    out = ScreenShotInjectMiddleware(session).before_model({}, None)
    return (out or {}).get("messages") or []


# ── 基本注入 ─────────────────────────────────────────────────

def test_view_image_injects_into_context(session, workspace, monkeypatch):
    _force_vision(monkeypatch)
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    r = tools["view_image"].invoke({"source": "plot.png", "note": "确认图表"})
    assert "图片已读入上下文" in r
    assert len(session._pending_screenshots) == 1
    entry = session._pending_screenshots[0]
    assert entry["url"].startswith("data:image/")
    assert "plot.png" in entry["text"] and "确认图表" in entry["text"]
    # 注入 → user 消息（text + image_url 块）+ viewed 元数据（去重用，不进端点）
    msgs = _drain_injected(session)
    assert len(msgs) == 1
    injected = msgs[0]
    assert injected.type == "human"
    kinds = [(b.get("type") if isinstance(b, dict) else "") for b in injected.content]
    assert kinds == ["text", "image_url"]
    assert injected.additional_kwargs.get("viewed_image"), "应带 viewed 去重键"
    assert session._pending_screenshots == []


def test_view_image_dedup_and_reinject_after_removed(session, workspace, monkeypatch):
    _force_vision(monkeypatch)
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    tools["view_image"].invoke({"source": "plot.png"})
    msgs = _drain_injected(session)
    # 注入的消息落进 buffer（模拟生成结束回写）→ 再读同一张图：不重复注入
    session.recent_buffer = list(msgs)
    r2 = tools["view_image"].invoke({"source": "plot.png"})
    assert "已经在" in r2 and not session._pending_screenshots
    # 被压缩/移除后 → 允许重新读入
    session.recent_buffer = []
    tools["view_image"].invoke({"source": "plot.png"})
    assert len(session._pending_screenshots) == 1


def test_view_image_absolute_path_without_workspace(session, workspace, monkeypatch):
    _force_vision(monkeypatch)
    tools = _tools(session)
    # 无工作区：绝对路径可读（只读工具既有行为）；相对路径给提示
    r = tools["view_image"].invoke({"source": str(workspace / "plot.png")})
    assert "图片已读入上下文" in r
    r2 = tools["view_image"].invoke({"source": "plot.png"})
    assert "需要先启用代码工作区" in r2


# ── 拒绝分支 ─────────────────────────────────────────────────

def test_view_image_rejections(session, workspace, tmp_path, monkeypatch):
    _force_vision(monkeypatch)
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    assert "不能为空" in tools["view_image"].invoke({"source": ""})
    assert "不存在" in tools["view_image"].invoke({"source": "nope.png"})
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    assert "不是支持的图片格式" in tools["view_image"].invoke({"source": "a.txt"})
    (workspace / "big.png").write_bytes(b"\0" * (15 * 1024 * 1024 + 1))
    assert "过大" in tools["view_image"].invoke({"source": "big.png"})
    # 黑名单（id_rsa*）
    (workspace / "id_rsa.png").write_bytes(PNG_1PX)
    assert "保护名单" in tools["view_image"].invoke({"source": "id_rsa.png"})


def test_view_image_outside_workspace_needs_approval(session, workspace, tmp_path):
    """区外读：未开启「允许工作区外读取」→ 审批中间件直接拒；区内放行。"""
    from planner.middleware import ApprovalMiddleware

    session.update_settings({"workspace_root": str(workspace)})
    outside = tmp_path / "out.png"
    outside.write_bytes(PNG_1PX)
    mw = ApprovalMiddleware(session)
    verdict, msg = mw._authorize("view_image", {"source": str(outside)})
    assert verdict == "deny" and ("未开启" in msg or "未授权" in msg)
    verdict2, _ = mw._authorize("view_image", {"source": "plot.png"})
    assert verdict2 == "allow"


# ── tool_targets ─────────────────────────────────────────────

def test_tool_targets_view_image(tmp_path):
    from planner.workspace import tool_targets

    ws = tmp_path / "ws"
    ws.mkdir()
    targets = tool_targets("view_image", {"source": "sub/a.png"}, ws)
    assert targets and targets[0].name == "a.png"
    assert tool_targets("view_image", {"source": "  "}, ws) is None


# ── OCR 回退 ─────────────────────────────────────────────────

def test_view_image_ocr_fallback(session, workspace, monkeypatch):
    # mock 模型 → vision_capable False → OCR 路径
    monkeypatch.setattr("planner.ocr.ocr_image_file", lambda p: "图表上的文字")
    monkeypatch.setattr("planner.ocr.ocr_enabled", lambda: True)
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    r = tools["view_image"].invoke({"source": "plot.png"})
    assert "OCR 识别文字" in r and "图表上的文字" in r
    assert not session._pending_screenshots


def test_view_image_ocr_empty_hint(session, workspace, monkeypatch):
    monkeypatch.setattr("planner.ocr.ocr_image_file", lambda p: None)
    monkeypatch.setattr("planner.ocr.ocr_enabled", lambda: True)
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    r = tools["view_image"].invoke({"source": "plot.png"})
    assert "不支持视觉" in r and "OCR 未识别到文字" in r
