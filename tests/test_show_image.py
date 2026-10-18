"""show_image：本地图片展示 + 网络图片下载缓存 + 事件 / 历史 / /file 端点。"""

from __future__ import annotations

import base64
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from planner.session import PlannerSession

# 1x1 透明 PNG
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


def _emit_and_get_image_events(session, tool_call_id="ti"):
    from langchain_core.messages import ToolMessage
    msg = ToolMessage(content="已展示", name="show_image", tool_call_id=tool_call_id)
    session._emit_tool_events(msg)
    events = session.drain_events()
    return msg, [e for e in events if e["type"] == "image"]


# ── 本地图片 ─────────────────────────────────────────────────

def test_show_image_local_ok(session, workspace):
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    r = tools["show_image"].invoke({"source": "plot.png", "caption": "我的图表"})
    assert "已展示图片给用户：plot.png" in r
    msg, imgs = _emit_and_get_image_events(session)
    assert len(imgs) == 1
    ev = imgs[0]
    assert ev["url"].startswith("/file?path=") and ev["caption"] == "我的图表"
    assert msg.additional_kwargs.get("image_paths"), "路径应写进消息（历史重建用）"


def test_show_image_rejects_bad_inputs(session, workspace, tmp_path):
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    # 工作区外
    outside = tmp_path / "out.png"
    outside.write_bytes(PNG_1PX)
    assert "工作区外" in tools["show_image"].invoke({"source": str(outside)})
    # 不存在
    assert "不存在" in tools["show_image"].invoke({"source": "nope.png"})
    # 非图片格式
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    assert "不是支持的图片格式" in tools["show_image"].invoke({"source": "a.txt"})
    # 过大
    (workspace / "big.png").write_bytes(b"\0" * (15 * 1024 * 1024 + 1))
    assert "过大" in tools["show_image"].invoke({"source": "big.png"})
    # 空 source
    assert "不能为空" in tools["show_image"].invoke({"source": "  "})


# ── 网络图片 ─────────────────────────────────────────────────

class _ImgHandler(BaseHTTPRequestHandler):
    def do_GET(self):   # noqa: N802
        if self.path == "/img":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_1PX)))
            self.end_headers()
            self.wfile.write(PNG_1PX)
        else:
            body = b"<html>not an image</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def img_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ImgHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_show_image_remote_downloads_and_caches(session, workspace, img_server,
                                                data_root):
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    url = f"{img_server}/img"     # 无扩展名 → 靠 content-type 判定
    r = tools["show_image"].invoke({"source": url, "caption": "网上找的图"})
    assert "已展示网络图片给用户" in r
    cache = Path(data_root) / "images"
    files = list(cache.glob("*.png"))
    assert files, "网络图片应下载到 data/images 缓存"
    assert files[0].read_bytes() == PNG_1PX
    _msg, imgs = _emit_and_get_image_events(session)
    assert imgs and imgs[0]["url"].startswith("/file?path=")
    # 缓存文件可通过 /file 提供
    assert str(files[0]) in urllib.parse.unquote(imgs[0]["url"])


def test_show_image_remote_non_image_rejected(session, workspace, img_server):
    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    r = tools["show_image"].invoke({"source": f"{img_server}/page"})
    assert "不是图片" in r
    assert not session._pending_images


# ── /file 端点 + /history 重建 ────────────────────────────────

def _start_backend(session):
    from planner.server import create_server
    httpd = create_server(session, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_file_endpoint_serves_and_guards(session, workspace, tmp_path, data_root):
    session.update_settings({"workspace_root": str(workspace)})
    httpd, base = _start_backend(session)
    try:
        from urllib.parse import quote
        url = base + "/file?path=" + quote(str((workspace / "plot.png").resolve()))
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
            assert r.read() == PNG_1PX
        # 工作区外 → 403
        outside = tmp_path / "out.png"
        outside.write_bytes(PNG_1PX)
        req = urllib.request.Request(
            base + "/file?path=" + quote(str(outside.resolve())))
        with pytest.raises(urllib.error.HTTPError) as e1:
            urllib.request.urlopen(req, timeout=5)
        assert e1.value.code == 403
        # 非图片后缀 → 403
        (workspace / "x.txt").write_text("x", encoding="utf-8")
        req2 = urllib.request.Request(
            base + "/file?path=" + quote(str((workspace / "x.txt").resolve())))
        with pytest.raises(urllib.error.HTTPError) as e2:
            urllib.request.urlopen(req2, timeout=5)
        assert e2.value.code == 403
        # data/images 缓存目录允许
        cache = Path(data_root) / "images"
        cache.mkdir(exist_ok=True)
        (cache / "c.png").write_bytes(PNG_1PX)
        with urllib.request.urlopen(
                base + "/file?path=" + quote(str((cache / "c.png").resolve())),
                timeout=5) as r3:
            assert r3.status == 200
    finally:
        httpd.shutdown()


def test_history_returns_image_entries(session, workspace):
    from langchain_core.messages import ToolMessage

    session.update_settings({"workspace_root": str(workspace)})
    tools = _tools(session)
    tools["show_image"].invoke({"source": "plot.png", "caption": "历史里的图"})
    msg = ToolMessage(content="已展示", name="show_image", tool_call_id="th")
    session._emit_tool_events(msg)
    session.recent_buffer = [msg]
    httpd, base = _start_backend(session)
    try:
        with urllib.request.urlopen(base + "/history", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        imgs = [m for m in data["messages"] if m["role"] == "image"]
        assert imgs and imgs[0]["caption"] == "历史里的图"
        assert imgs[0]["url"].startswith("/file?path=")
    finally:
        httpd.shutdown()
