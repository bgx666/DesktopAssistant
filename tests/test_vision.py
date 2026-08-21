"""视觉能力测试：图片注入（image_url 块）/ 非视觉回退 OCR / 图片剥离 / 序列化。"""

import base64
import json
import threading

import numpy as np
from langchain_core.messages import HumanMessage, messages_from_dict, messages_to_dict

from planner.content import content_text
from planner.llm import is_vision_model, resolve_model_name
from planner.session import PlannerSession


def _make_png_bytes(size: int = 64) -> bytes:
    """生成一张纯色 PNG（cv2 编码），用于视觉注入测试。"""
    import cv2
    img = np.full((size, size, 3), 120, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _write_png(tmp_path, name="shot.png", size: int = 64):
    p = tmp_path / name
    p.write_bytes(_make_png_bytes(size))
    return p


def _image_file(tmp_path, name="shot.png") -> dict:
    p = _write_png(tmp_path, name)
    return {"name": name, "path": str(p), "kind": "image", "content": None}


def _force_vision(s, monkeypatch):
    """mock 会话强制启用视觉（绕过 mock 恒关的约束）。"""
    monkeypatch.setattr(PlannerSession, "vision_capable", property(lambda self: True))
    return s


# ── 模型解析 ─────────────────────────────────────────────────

def test_resolve_model_default_vision(monkeypatch):
    """未配置任何项 → 默认 vision-exp；PLANNER_LLM_MODEL 优先；共享 LLM_MODEL 不生效。"""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
    assert resolve_model_name() == "deepseek-v4-flash-vision-exp"
    monkeypatch.setenv("PLANNER_LLM_MODEL", "planner-own-model")
    monkeypatch.setenv("LLM_MODEL", "shared-model")
    assert resolve_model_name() == "planner-own-model"
    assert resolve_model_name("settings-model") == "settings-model"
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
    # 共享 LLM_MODEL（xiaob 的配置）不再影响 planner
    assert resolve_model_name() == "deepseek-v4-flash-vision-exp"


def test_is_vision_model():
    assert is_vision_model("deepseek-v4-flash-vision-exp")
    assert is_vision_model("gpt-4o")
    assert is_vision_model("gpt-4o-mini")      # 含 "gpt-4o" 子串（mini 也支持视觉）
    assert is_vision_model("qwen2.5-vl-7b")
    assert not is_vision_model("deepseek-v4-flash")
    assert not is_vision_model("deepseek-v4-pro")
    assert not is_vision_model("qwen2.5-7b")


# ── 图片注入 ─────────────────────────────────────────────────

def test_image_block_returns_data_url(data_root):
    """_image_block：图片路径 → image_url 块（base64 PNG data URL）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        block = s._image_block(str(_write_png(data_root)))
        assert block is not None
        assert block["type"] == "image_url"
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        raw = base64.b64decode(url.split(",", 1)[1])
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        s.close()


def test_image_block_bad_file_returns_none(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        bad = data_root / "broken.png"
        bad.write_bytes(b"not an image")
        assert s._image_block(str(bad)) is None
        assert s._image_block(str(data_root / "missing.png")) is None
    finally:
        s.close()


def test_render_attachments_vision_injection(data_root, monkeypatch):
    """视觉路径：_render_attachments 返回 {"text", "images"}，文本块标注已发原图。"""
    s = _force_vision(PlannerSession(data_root, mock=True), monkeypatch)
    try:
        out = s._render_attachments([_image_file(data_root)])
        assert isinstance(out, dict)
        assert len(out["images"]) == 1
        assert out["images"][0]["type"] == "image_url"
        assert "已发送原图" in out["text"]
    finally:
        s.close()


def test_render_attachments_ocr_fallback(data_root):
    """非视觉模型（mock 默认）→ 保持 OCR 文本路径，不产生图片块。"""
    s = PlannerSession(data_root, mock=True)
    try:
        out = s._render_attachments([_image_file(data_root)])
        assert isinstance(out, str)
        assert "image_url" not in out
    finally:
        s.close()


def test_render_attachments_vision_fallback_on_bad_image(data_root, monkeypatch):
    """视觉路径但图片读取失败 → 回退 OCR 文本（不丢附件）。"""
    s = _force_vision(PlannerSession(data_root, mock=True), monkeypatch)
    try:
        bad = data_root / "broken.png"
        bad.write_bytes(b"not an image")
        out = s._render_attachments([{"name": "broken.png", "path": str(bad),
                                      "kind": "image", "content": None}])
        assert isinstance(out, str)
    finally:
        s.close()


def test_enqueue_player_message_vision_content(data_root, monkeypatch):
    """enqueue_player_message（视觉）→ buffer 中 human 消息 content 为块列表。"""
    s = _force_vision(PlannerSession(data_root, mock=True), monkeypatch)
    try:
        s.chat_lock.acquire()   # 压住 worker 线程，避免生成回写 buffer 竞态
        try:
            s.enqueue_player_message("看看这张图", [_image_file(data_root)])
            m = s.recent_buffer[-1]
            assert isinstance(m.content, list)
            kinds = [b["type"] for b in m.content]
            assert "text" in kinds and "image_url" in kinds
            text = content_text(m.content)
            assert "对你说：看看这张图" in text
            assert "shot.png" in text
        finally:
            s.chat_lock.release()
    finally:
        s.close()


# ── 图片常驻 ─────────────────────────────────────────────────

def test_image_blocks_persist_after_generation(data_root, monkeypatch):
    """生成结束后图片块保留在 buffer（常驻整个会话，可随时回看）。

    与主流聊天一致：图片作为 image_url 块一直待在上下文里；/history 只序列化
    文本块，base64 不出现在历史接口。
    """
    s = _force_vision(PlannerSession(data_root, mock=True), monkeypatch)
    try:
        s._receive([
            {"type": "text", "text": "[10:00] 用户对你说：描述这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ], trigger=True)
        s.pending_response = False
        s._generate_response("player")   # mock 生成 + 落盘
        # 图片块仍保留（没有被剥离）
        img_msgs = [
            m for m in s.recent_buffer
            if isinstance(getattr(m, "content", ""), list)
            and any((b.get("type") if isinstance(b, dict) else "") == "image_url"
                    for b in m.content)
        ]
        assert img_msgs, "图片应常驻 buffer，可在后续回合继续回看"
    finally:
        s.close()


# ── 序列化 round-trip ────────────────────────────────────────

def test_buffer_state_roundtrip_list_content(data_root, monkeypatch):
    """列表 content（含 image_url 块）经 messages_to_dict 持久化后可恢复。"""
    s = _force_vision(PlannerSession(data_root, mock=True), monkeypatch)
    try:
        s.chat_lock.acquire()
        try:
            s.enqueue_player_message("看图", [_image_file(data_root)])
            blob = messages_to_dict(s.recent_buffer)
        finally:
            s.chat_lock.release()
        restored = messages_from_dict(json.loads(json.dumps(blob)))
        assert len(restored) == len(s.recent_buffer)
        m = restored[-1]
        assert isinstance(m.content, list)
        assert m.content[0]["type"] == "text"
        assert any(b["type"] == "image_url" for b in m.content)
    finally:
        s.close()


def test_content_text_list_and_string():
    """content_text：str 原样返回，列表只取 text 块，image_url 忽略。"""
    assert content_text("纯文本") == "纯文本"
    blocks = [
        {"type": "text", "text": "第一段"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "第二段"},
    ]
    assert content_text(blocks) == "第一段\n第二段"
    assert content_text([]) == ""
    assert content_text(None) == ""
    assert content_text(123) == "123"


def test_history_endpoint_with_image(data_root, monkeypatch):
    """/history 对列表 content 只输出文本（图片块不出现 JSON 串）。"""
    from planner.server import create_server

    s = _force_vision(PlannerSession(data_root, mock=True), monkeypatch)
    try:
        s.chat_lock.acquire()
        try:
            s.enqueue_player_message("看图", [_image_file(data_root)])
        finally:
            s.chat_lock.release()
        httpd = create_server(s, port=0)
        try:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            import urllib.request
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_address[1]}/history",
                    timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            httpd.server_close()
        user_msgs = [m for m in data["messages"] if m["role"] == "user"]
        assert user_msgs
        content = user_msgs[-1]["content"]
        assert "image_url" not in content        # 列表 content 只序列化文本块
        assert "看图" in content                 # 前缀已按惯例剥离，正文保留
        assert "shot.png" in content
        assert "data:image/png;base64" not in content   # base64 不落历史
    finally:
        s.close()


def test_vision_capable_respects_model(data_root, monkeypatch):
    """vision_capable：mock → False；默认模型 → True；纯文本模型 → False。"""
    s = PlannerSession(data_root, mock=True)
    try:
        assert not s.vision_capable   # mock 恒关
        monkeypatch.setattr(s, "mock", False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
        assert s.vision_capable       # 默认 vision-exp（LLM_MODEL=flash 也不受影响）
        monkeypatch.setenv("PLANNER_LLM_MODEL", "deepseek-v4-flash")
        assert not s.vision_capable
        monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
        s.settings["llm_model"] = "deepseek-v4-flash"
        assert not s.vision_capable
        s.settings["llm_model"] = "deepseek-v4-flash-vision-exp"
        assert s.vision_capable
    finally:
        s.close()
