"""HTTP 端点集成测试（mock 模式，真实起 HTTP server + 轮询 /dequeue）。"""

import json
import threading
import time
import urllib.request
from pathlib import Path

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


def test_dequeue_no_wait_immediate(backend):
    """不带 wait 参数：/dequeue 立即返回（旧行为，测试/旧客户端兼容）。"""
    _, base = backend
    t0 = time.time()
    data = _get(base, "/dequeue")
    assert data["ok"] and "events" in data
    assert time.time() - t0 < 1.0, "不带 wait 应立即返回"


def test_dequeue_longpoll_waits_timeout(backend):
    """/dequeue?wait=N 无事件时挂起约 N 秒后返回空（长轮询超时）。"""
    _, base = backend
    t0 = time.time()
    data = _get(base, "/dequeue?wait=0.3")
    assert data["ok"] and data["events"] == []
    elapsed = time.time() - t0
    assert 0.2 <= elapsed < 2.0, f"wait=0.3 应挂起约 0.3s，实际 {elapsed:.2f}s"


def test_dequeue_longpoll_wakes_on_event(backend):
    """长轮询挂起期间有新事件到达 → 立即返回该事件（接近零延迟推送）。"""
    session, base = backend
    threading.Timer(0.3, session.push_event,
                    args=({"type": "log", "text": "wake"},)).start()
    t0 = time.time()
    data = _get(base, "/dequeue?wait=2")
    elapsed = time.time() - t0
    assert data["ok"]
    assert any(e["type"] == "log" and e["text"] == "wake" for e in data["events"]), \
        "挂起期间到达的事件应立即返回"
    assert elapsed < 1.5, f"事件到达应唤醒长轮询，实际 {elapsed:.2f}s"


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
    """历史消息应包含工具调用卡片（tool_call + tool_result，含 heartbeat 定时唤醒）。"""
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


def test_tts_say_endpoint(backend):
    """/tts/say：空文本 400；引擎不可用 → 500，且不落音频文件。"""
    import urllib.error
    from urllib.parse import quote
    session, base = backend
    session.tts._engine_ok = False   # 模拟引擎不可用（测试不碰真实模型/网络）
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base, "/tts/say?text=")
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base, "/tts/say?text=" + quote("你好"))
    assert e.value.code == 500
    assert list(session.tts.tts_dir.glob("*")) == []


def test_tts_voices_endpoint(backend, monkeypatch):
    """/tts/voices：返回音色列表；/tts/say 校验非法音色。"""
    import urllib.error
    from urllib.parse import quote
    session, base = backend
    monkeypatch.setattr(session.tts, "list_voices", lambda: [
        {"id": "zf_001", "label": "zf_001 · 女声"},
        {"id": "zm_009", "label": "zm_009 · 男声"},
    ])
    r = _get(base, "/tts/voices")
    assert r["ok"] is True
    assert [v["id"] for v in r["voices"]] == ["zf_001", "zm_009"]
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base, "/tts/say?text=" + quote("你好") + "&voice=bad_voice")
    assert e.value.code == 400


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


def test_chat_with_files_injection(backend):
    """/chat 带 files → 注入消息含附件（text 内容直注 / image OCR / doc 解析落盘）。"""
    import io as _io
    import wave as _wave

    session, base = backend
    _get(base, "/dequeue")

    # 1) text：内容直注
    r = _post(base, "/chat", {
        "message": "看看这个",
        "files": [{"name": "a.txt", "path": "D:\\x\\a.txt", "kind": "text", "content": "文件内容ABC"}],
    })
    assert r["ok"]
    deadline = time.time() + 8
    while time.time() < deadline:
        m = next((x for x in session.recent_buffer
                  if getattr(x, "type", "") == "human" and "文件内容ABC" in str(x.content)), None)
        if m:
            break
        time.sleep(0.1)
    assert m is not None, "text 附件内容应注入"
    assert "【拖入的文件】" in str(m.content)
    _get(base, "/dequeue")

    # 2) image：OCR 提取（mock OcrClient）
    class _FakeOcr:
        def recognize_path(self, path):
            return "图片上的文字"

    session.asr = session.asr  # noqa: B018 保持引用
    import planner.session as _sm
    orig_ocr = _sm._global_ocr if hasattr(_sm, "_global_ocr") else None
    # 直接替换 session 上的注入路径：monkeypatch ocr 全局单例
    import planner.ocr as _ocr_mod
    fake_ocr = _FakeOcr()
    monkeypatch_ocr = None
    try:
        _ocr_mod._global = fake_ocr
        r = _post(base, "/chat", {
            "message": "",
            "files": [{"name": "p.png", "path": "D:\\x\\p.png", "kind": "image"}],
        })
        assert r["ok"]
        deadline = time.time() + 8
        m2 = None
        while time.time() < deadline:
            m2 = next((x for x in session.recent_buffer
                       if getattr(x, "type", "") == "human" and "图片上的文字" in str(x.content)), None)
            if m2:
                break
            time.sleep(0.1)
        assert m2 is not None, "image 附件应 OCR 注入"
        assert "【图片 OCR 识别文字】" in str(m2.content)
    finally:
        _ocr_mod._global = None

    # 3) doc：解析落盘 + 预览（monkeypatch fileparse）
    import planner.fileparse as _fp
    real_parse = _fp.parse_file
    real_save = _fp.save_attachment_text
    try:
        _fp.parse_file = lambda path: "解析出的文档内容"
        _fp.save_attachment_text = lambda root, src, text: Path(root) / "attachments" / "fake.txt"
        r = _post(base, "/chat", {
            "message": "",
            "files": [{"name": "r.pdf", "path": "D:\\x\\r.pdf", "kind": "doc"}],
        })
        assert r["ok"]
        deadline = time.time() + 8
        m3 = None
        while time.time() < deadline:
            m3 = next((x for x in session.recent_buffer
                       if getattr(x, "type", "") == "human" and "解析出的文档内容" in str(x.content)), None)
            if m3:
                break
            time.sleep(0.1)
        assert m3 is not None, "doc 附件应解析注入"
        assert "read_file" in str(m3.content)
    finally:
        _fp.parse_file = real_parse
        _fp.save_attachment_text = real_save


def test_settings_endpoint(backend):
    """GET/POST /settings：读取、保存、校验失败 400。"""
    session, base = backend
    d = _get(base, "/settings")
    assert d["ok"] and d["settings"]["press_ms"] == 200
    r = _post(base, "/settings", {"updates": {"press_ms": 300, "compress_trigger": 80}})
    assert r["ok"] and r["settings"]["press_ms"] == 300
    assert session.settings["compress_trigger"] == 80
    from urllib.error import HTTPError
    try:
        _post(base, "/settings", {"updates": {"press_ms": 1}})
        assert False, "非法值应 400"
    except HTTPError as e:
        assert e.code == 400


def test_toggle_mock(backend):
    _, base = backend
    r = _post(base, "/toggle_mock")
    assert r["mode"] == "llm"
    r2 = _post(base, "/toggle_mock")
    assert r2["mode"] == "mock"
