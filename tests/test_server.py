"""HTTP 端点集成测试（mock 模式，真实起 HTTP server + 轮询 /dequeue）。"""

import json
import threading
import time
import urllib.request

import pytest

from planner.server import create_server
from planner.session import PlannerSession


@pytest.fixture
def backend(data_root):
    """起一个 mock 模式后端（随机端口，daemon 线程）。"""
    session = PlannerSession(data_root, mock=True)
    httpd = create_server(session, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield session, base
    httpd.shutdown()
    session.close()


def _get(base, path, _retries=2):
    for attempt in range(_retries + 1):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except (ConnectionError, OSError):
            if attempt >= _retries:
                raise
            time.sleep(0.3)


def _post(base, path, body=None, _retries=2):
    data = json.dumps(body or {}).encode("utf-8")
    for attempt in range(_retries + 1):
        try:
            req = urllib.request.Request(base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except (ConnectionError, OSError):
            if attempt >= _retries:
                raise
            time.sleep(0.3)


def _wait_events(base, pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _get(base, "/dequeue")
        for ev in data["events"]:
            if pred(ev):
                return ev
        time.sleep(0.1)
    return None


def test_init_and_state(backend):
    _, base = backend
    data = _get(base, "/init")
    assert data["ok"] and data["backend"].startswith("planner/")
    assert data["mode"] == "mock"
    assert data["char"]["display_name"] == "小助"
    st = _get(base, "/state")["state"]
    assert "heartbeat" in st and "dnd" in st and "plan" in st


def test_chat_produces_text_event(backend):
    _, base = backend
    r = _post(base, "/chat", {"message": "你好，帮我安排学习"})
    assert r["ok"] is True
    ev = _wait_events(base, lambda e: e["type"] == "text")
    assert ev is not None and ev["content"]


def test_chat_empty_rejected(backend):
    _, base = backend
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _post(base, "/chat", {"message": ""})


def test_undo_endpoint(backend):
    """/chat 返回 msg_id → /undo 删除该消息及其后对话 → /history 变空。"""
    session, base = backend
    r = _post(base, "/chat", {"message": "这条消息稍后要撤销"})
    assert r["ok"] is True
    msg_id = r.get("msg_id")
    assert msg_id, "chat 应返回 msg_id"
    _wait_events(base, lambda e: e["type"] == "text")

    hist = _get(base, "/history")
    ids = [m.get("id") for m in hist["messages"] if m.get("role") == "user"]
    assert msg_id in ids, "history 应携带用户消息 id"

    u = _post(base, "/undo", {"msg_id": msg_id})
    assert u["ok"] is True
    hist2 = _get(base, "/history")
    assert all(m.get("id") != msg_id for m in hist2["messages"]), "撤销后该消息应消失"
    # 已撤销（不存在）的消息再次撤销 → compressed
    u2 = _post(base, "/undo", {"msg_id": msg_id})
    assert u2["ok"] is False and u2.get("reason") == "compressed"


def test_history_contains_tool_cards(backend):
    """历史消息应包含工具调用卡片（tool_call + tool_result，heartbeat 除外）。"""
    _, base = backend
    r = _post(base, "/chat", {"message": "帮我安排学习计划"})
    assert r["ok"] is True
    _wait_events(base, lambda e: e["type"] == "text")

    hist = _get(base, "/history")
    roles = [m["role"] for m in hist["messages"]]
    assert "tool_call" in roles, "历史应包含工具调用卡片"
    assert "tool_result" in roles, "历史应包含工具结果"
    calls = [m for m in hist["messages"] if m["role"] == "tool_call"]
    results = [m for m in hist["messages"] if m["role"] == "tool_result"]
    # 每个 tool_call 都有对应 id 的 tool_result（工具已完成）
    for c in calls:
        assert any(rr["id"] == c["id"] for rr in results), "tool_call 应有对应 tool_result"
        assert c["name"] != "heartbeat", "heartbeat 不展示"
    assert any(rr.get("content") for rr in results), "工具结果应有内容"


def test_history_shows_memory_nodes(backend):
    """压缩节点（node_id）应在历史中显示为 memory 消息。"""
    session, base = backend
    from langchain_core.messages import HumanMessage
    session.recent_buffer.insert(
        0, HumanMessage(content="[node0_001] 第0-40条\n[摘要] 早期对话摘要",
                        metadata={"node_id": "node0_001"}))
    hist = _get(base, "/history")
    memory = [m for m in hist["messages"] if m["role"] == "memory"]
    assert len(memory) == 1, "历史应显示压缩节点"
    assert memory[0]["node_id"] == "node0_001"
    assert "摘要" in memory[0]["content"]


def test_task_endpoints(backend):
    _, base = backend
    r = _post(base, "/task", {"title": "写论文", "due_date": "2026-09-01", "priority": "high"})
    assert r["ok"] and r["id"] >= 1
    tasks = _get(base, "/tasks")["tasks"]
    assert any(t["title"] == "写论文" for t in tasks)
    plan = _get(base, "/plan")["plan"]
    assert isinstance(plan, list)


def test_plan_done_roundtrip(backend):
    session, base = backend
    tid = session.db.create_task("任务")
    pid = session.db.add_plan_item(tid, None, "2026-08-05", 0, "做某事")
    r = _post(base, "/plan/done", {"plan_id": pid})
    assert r["ok"] is True
    assert session.db.get_task(tid)["plan_items"][0]["status"] == "done"
    ev = _wait_events(base, lambda e: e["type"] == "text")
    assert ev is not None  # agent 收到勾选通知后有回应


def test_dnd_endpoint(backend):
    session, base = backend
    r = _post(base, "/dnd", {"enabled": True, "until_hour": 14})
    assert r["dnd"]["enabled"] is True
    assert session.dnd_until.hour == 14
    _post(base, "/dnd", {"enabled": False})
    assert session.dnd_until is None


def test_nudge(backend):
    _, base = backend
    r = _post(base, "/nudge")
    assert r["ok"] is True
    ev = _wait_events(base, lambda e: e["type"] == "text")
    assert ev is not None


def test_dequeue_drains(backend):
    _, base = backend
    _get(base, "/dequeue")  # 清空
    r = _post(base, "/nudge")
    assert r["ok"]
    _wait_events(base, lambda e: e["type"] == "text")
    # 事件最终会 drain 空（text 之后还有 thinking/plan_update 尾巴事件）
    deadline = time.time() + 5
    while time.time() < deadline:
        data = _get(base, "/dequeue")
        if data["events"] == []:
            return
        time.sleep(0.1)
    assert False, "事件未 drain 空"


def test_tts_endpoint(backend):
    """GET /tts/{name} 返回音频字节；非法名/不存在 → 404。"""
    session, base = backend
    name = "a" * 32 + ".mp3"
    session.tts.tts_dir.mkdir(parents=True, exist_ok=True)
    (session.tts.tts_dir / name).write_bytes(b"MP3DATA")
    with urllib.request.urlopen(base + "/tts/" + name, timeout=5) as r:
        assert r.status == 200
        assert r.read() == b"MP3DATA"
    from urllib.error import HTTPError
    for bad in ("x.mp3", "a" * 31 + ".mp3", "b" * 32 + ".wav", "..%2Fpasswd"):
        try:
            urllib.request.urlopen(base + "/tts/" + bad, timeout=5)
            assert False, f"非法名 {bad} 应 404"
        except HTTPError as e:
            assert e.code == 404


def test_asr_endpoint(backend):
    """POST /asr（wav 二进制）→ 识别文本；识别失败 → 502；空 body → 400。"""
    import struct
    import io as _io
    import wave as _wave

    session, base = backend

    class _FakeAsr:
        enabled = True
        ready = True

        def __init__(self):
            self.last = None

        def recognize(self, wav_bytes):
            self.last = wav_bytes
            return "你好，小助。"

    fake = _FakeAsr()
    session.asr = fake

    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<8000h", *([100] * 8000)))
    body = buf.getvalue()

    req = urllib.request.Request(base + "/asr", data=body,
                                 headers={"Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    assert d == {"ok": True, "text": "你好，小助。"}
    assert fake.last == body

    # 识别失败 → 502
    session.asr = type("F", (), {"recognize": lambda self, b: None, "enabled": True})()
    from urllib.error import HTTPError
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/asr", data=body), timeout=10)
        assert False, "识别失败应 502"
    except HTTPError as e:
        assert e.code == 502
    # 空 body → 400
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/asr", data=b""), timeout=10)
        assert False, "空 body 应 400"
    except HTTPError as e:
        assert e.code == 400


def test_init_exposes_asr(backend):
    """/init 暴露 asr.enabled/ready，前端据此决定是否显示语音入口。"""
    from types import SimpleNamespace
    session, base = backend
    session.asr = SimpleNamespace(enabled=True, ready=False)
    d = _get(base, "/init")
    assert d["asr"] == {"enabled": True, "ready": False}


def test_toggle_mock(backend):
    _, base = backend
    r = _post(base, "/toggle_mock")
    assert r["mode"] == "llm"
    r2 = _post(base, "/toggle_mock")
    assert r2["mode"] == "mock"
