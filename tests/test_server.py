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
    assert "heartbeat" in st and "agenda" in st


def test_chat_produces_text_event(backend):
    _, base = backend
    r = _post(base, "/chat", {"message": "你好，帮我安排学习"})
    assert r["ok"] is True
    ev = _wait_events(base, lambda e: e["type"] == "text")
    assert ev is not None and ev["content"]


def test_chat_pushes_user_message_event(backend):
    """/chat 先推 user_message（悬浮球语音/快速输入发的消息，面板靠它渲染气泡）。"""
    _, base = backend
    r = _post(base, "/chat", {"message": "悬浮球发的消息"})
    ev = _wait_events(base, lambda e: e["type"] == "user_message")
    assert ev is not None
    assert ev["text"] == "悬浮球发的消息"
    assert ev["id"] == r["msg_id"]


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


def test_history_keeps_tool_card_without_text(backend):
    """**正文为空、只调工具**的 AI 消息，其工具卡片也必须出现在历史里。

    回归：/history 原来对每条消息先做 `if not content: continue`，而模型
    "直接调工具、不先说话"时 content 是空的 → 卡片被吞掉。前端 onPanelShown
    会清空消息区再按 /history 重建，所以表现是"展开面板后工具卡片有时候不见了"。
    """
    session, base = backend
    from langchain_core.messages import AIMessage, ToolMessage
    session.recent_buffer.append(AIMessage(
        content="",   # 关键：不先说话，直接调工具
        tool_calls=[{"name": "web_search", "args": {"query": "天气"},
                     "id": "call_empty_text", "type": "tool_call"}]))
    session.recent_buffer.append(ToolMessage(
        content="搜索结果：晴", name="web_search", tool_call_id="call_empty_text"))

    hist = _get(base, "/history")
    calls = [m for m in hist["messages"] if m["role"] == "tool_call"]
    results = [m for m in hist["messages"] if m["role"] == "tool_result"]
    assert any(c["id"] == "call_empty_text" for c in calls), \
        "空正文的工具调用卡片不能丢（否则展开面板就看不见了）"
    assert any(r["id"] == "call_empty_text" for r in results), "工具结果也应保留"
    # 空正文不应产生一条空的 assistant 气泡
    assert not any(m["role"] == "assistant" and not m.get("content") for m in hist["messages"])


def test_history_skips_continue_speaking_cards(backend):
    """continue_speaking 与实时事件保持一致：不产生工具卡片（视觉上就是连续说话）。"""
    session, base = backend
    from langchain_core.messages import AIMessage, ToolMessage
    session.recent_buffer.append(AIMessage(
        content="",
        tool_calls=[{"name": "continue_speaking", "args": {},
                     "id": "call_cs", "type": "tool_call"}]))
    session.recent_buffer.append(ToolMessage(
        content="继续", name="continue_speaking", tool_call_id="call_cs"))

    hist = _get(base, "/history")
    assert not any(m.get("id") == "call_cs" for m in hist["messages"]), \
        "分段工具不应出现卡片"
    assert not any(m["role"] == "tool_result" and m.get("id") == "call_cs"
                   for m in hist["messages"])


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


def test_removed_task_endpoints_are_gone(backend):
    """任务模块整体移除后，那批端点必须返回 404（不留半死接口）。"""
    from urllib.error import HTTPError
    _, base = backend
    for path in ("/tasks", "/plan", "/next"):
        with pytest.raises(HTTPError) as e:
            _get(base, path)
        assert e.value.code == 404, f"{path} 应已移除"
    for path in ("/task", "/plan/done"):
        with pytest.raises(HTTPError) as e:
            _post(base, path, {"title": "x", "plan_id": 1})
        assert e.value.code == 404, f"{path} 应已移除"


def test_state_reports_agenda(backend):
    """状态里给前端的是日历口径的 agenda（今天几项、逾期几项）。"""
    session, base = backend
    session.db.add_calendar_item("2026-09-11", "交房租", "10:00")
    st = _get(base, "/state")["state"]
    assert "plan" not in st, "旧的 plan 口径应已移除"
    ag = st["agenda"]
    assert set(ag) == {"today", "today_total", "today_todo", "overdue_count"}


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
    """/tts/voices：返回音色列表（并把 ?engine= 透传下去）；/tts/say 校验非法音色。"""
    import urllib.error
    from urllib.parse import quote
    session, base = backend
    seen_engines: list[str | None] = []

    # 桩要接受 engine 关键字：端点会按 ?engine= 预览指定引擎的音色列表。
    # 早先这个 lambda 不收参数，端点一调就 TypeError、连接被关，
    # 报出来是 RemoteDisconnected（看着像网络问题，其实是桩过期了）。
    def fake_list_voices(engine=None):
        seen_engines.append(engine)
        return [
            {"id": "zf_001", "label": "zf_001 · 女声"},
            {"id": "zm_009", "label": "zm_009 · 男声"},
        ]

    monkeypatch.setattr(session.tts, "list_voices", fake_list_voices)
    r = _get(base, "/tts/voices")
    assert r["ok"] is True
    assert [v["id"] for v in r["voices"]] == ["zf_001", "zm_009"]
    assert seen_engines == [None]

    r = _get(base, "/tts/voices?engine=mimo")
    assert [v["id"] for v in r["voices"]] == ["zf_001", "zm_009"]
    assert seen_engines[-1] == "mimo", "?engine= 要透传给 list_voices"

    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base, "/tts/say?text=" + quote("你好") + "&voice=bad_voice")
    assert e.value.code == 400


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
    r = _post(base, "/settings", {"updates": {"press_ms": 300, "compress_token_budget": 20000}})
    assert r["ok"] and r["settings"]["press_ms"] == 300
    assert session.settings["compress_token_budget"] == 20000
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


def test_llm_providers_endpoint(backend, monkeypatch):
    """模型服务商端点：预设清单 + 当前配置；未知 provider → 400；无 key 时回退预设。"""
    from urllib.error import HTTPError

    _, base = backend
    r = _get(base, "/llm/providers")
    assert r["ok"]
    assert {p["id"] for p in r["providers"]} == {"deepseek", "opencode"}
    assert all("has_key" in p for p in r["providers"])
    assert r["current"]["provider"] == "deepseek"
    assert r["current"]["model"] == "deepseek-flash"
    assert r["current"]["base_url"] == "https://api.deepseek.com"

    try:
        _get(base, "/llm/models?provider=anthropic")
        assert False, "未知 provider 应 400"
    except HTTPError as e:
        assert e.code == 400

    # 无 key（环境变量也清掉）→ 不发起网络，回退预设清单
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = _get(base, "/llm/models?provider=opencode")
    assert r["ok"] and r["source"] == "preset"
    assert "deepseek-v4.1-flash" in r["models"]


def test_workspace_cwd_dirs_files_endpoints(backend, tmp_path):
    """第二轮端点：/workspace/cwd 切换、/workspace/dirs 列子目录、/workspace/files 补全。"""
    from urllib.error import HTTPError

    session, base = backend
    ws = tmp_path / "ws"
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (ws / "README.md").write_text("hi\n", encoding="utf-8")

    r = _post(base, "/workspace", {"path": str(ws)})
    assert r["ok"] and r["cwd"] == str(ws.resolve())

    # 切到子目录
    r = _post(base, "/workspace/cwd", {"path": str(ws / "src")})
    assert r["ok"] and r["cwd"] == str((ws / "src").resolve())
    assert r["recent"][0] == str((ws / "src").resolve())

    # 越界 → 400
    try:
        _post(base, "/workspace/cwd", {"path": str(tmp_path)})
        assert False, "工作区外目录应 400"
    except HTTPError as e:
        assert e.code == 400

    # GET /workspace 带 cwd/mode/recent
    r = _get(base, "/workspace")
    assert r["cwd"] == str((ws / "src").resolve())
    assert r["permission_mode"] == "ask"
    assert r["recent"]

    # 子目录列表
    r = _get(base, "/workspace/dirs")
    assert r["ok"] and r["dirs"] == ["pkg"]
    # 列工作区根（相对当前目录用 ..）
    r = _get(base, "/workspace/dirs?path=" + str(ws))
    assert "src" in r["dirs"]

    # @ 补全：列出当前目录（含子目录）的文件，q 过滤
    r = _get(base, "/workspace/files")
    assert "main.py" in r["files"] and "pkg/main.py" not in r["files"]
    r = _get(base, "/workspace/files?q=main")
    assert r["files"] == ["main.py"]

    # 权限模式：走 /settings 枚举校验
    r = _post(base, "/settings", {"updates": {"permission_mode": "auto"}})
    assert r["ok"] and r["settings"]["permission_mode"] == "auto"
    assert _get(base, "/workspace")["permission_mode"] == "auto"
    try:
        _post(base, "/settings", {"updates": {"permission_mode": "yolo"}})
        assert False, "非法权限模式应 400"
    except HTTPError as e:
        assert e.code == 400


def test_workspace_and_approval_endpoints(backend, tmp_path):
    """代码工作区端点：GET/POST /workspace（含非法目录 400）+ POST /approval。"""
    from urllib.error import HTTPError

    session, base = backend
    ws = tmp_path / "ws"
    ws.mkdir()

    r = _get(base, "/workspace")
    assert r["ok"] and r["enabled"] is False and r["workspace"] == ""

    r = _post(base, "/workspace", {"path": str(ws)})
    assert r["ok"] and r["enabled"] is True
    assert Path(r["workspace"]) == ws.resolve()
    assert session.settings["workspace_root"] == str(ws.resolve())

    # 非法目录 → 400
    try:
        _post(base, "/workspace", {"path": str(tmp_path / "nope")})
        assert False, "不存在的目录应 400"
    except HTTPError as e:
        assert e.code == 400

    # 清空 = 关闭
    r = _post(base, "/workspace", {"path": ""})
    assert r["ok"] and r["enabled"] is False

    # /approval：未知 id → 404；回填命中挂起请求 → 200
    try:
        _post(base, "/approval", {"id": "no-such", "decision": "deny"})
        assert False, "未知审批 id 应 404"
    except HTTPError as e:
        assert e.code == 404

    result = {}

    def worker():
        result["decision"] = session.request_approval("write_file", ["D:/x/a.py"])

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    req = _wait_events(base, lambda ev: ev.get("type") == "approval_request", timeout=5)
    assert req and req["tool"] == "write_file"
    r = _post(base, "/approval", {"id": req["id"], "decision": "allow_once"})
    assert r["ok"] is True
    t.join(timeout=3)
    assert result.get("decision") == "allow_once"


# ── 启动自检：残留后端识别（纯函数）──────────────────────────

def test_is_planner_cmdline():
    from planner.server import is_planner_cmdline

    assert is_planner_cmdline(r"D:\venv\pythonw.exe -m planner")
    assert is_planner_cmdline("python -m planner --port 18772")
    assert not is_planner_cmdline(r"C:\other\app.exe --serve")
    assert not is_planner_cmdline(None)
    assert not is_planner_cmdline("")


def test_should_kill_stale_decision():
    """杀残留的三条件：有 PID 记录 / 不是自己 / 命令行验明是 planner。"""
    from planner.server import should_kill_stale

    assert should_kill_stale(123, 456, "python -m planner")
    assert not should_kill_stale(123, 456, "notepad.exe")        # 无关进程不误杀
    assert not should_kill_stale(None, 456, "python -m planner")  # 无记录不动
    assert not should_kill_stale(123, 123, "python -m planner")   # 自己不动


def test_changes_and_permissions_endpoints(backend, tmp_path):
    """/changes 列表 + /changes/rollback 回退 + 规则管理端点（删除/清空）。"""
    from planner.workspace import PermissionStore

    session, base = backend
    ws = tmp_path / "wsc"
    ws.mkdir()
    _post(base, "/workspace", {"path": str(ws)})
    tools = {t.name: t for t in __import__(
        "planner.tools", fromlist=["build_tools"]).build_tools(session)}
    tools["write_file"].invoke({"path": "x.txt", "content": "abc"})
    r = _get(base, "/changes")
    assert r["ok"] and r["changes"] and r["changes"][0]["rel"] == "x.txt"
    cid = r["changes"][0]["id"]
    r2 = _post(base, "/changes/rollback", {"id": cid})
    assert r2["ok"] and r2["reverted"] == [cid]
    assert not (ws / "x.txt").exists()
    assert _post(base, "/changes/clear")["ok"]

    # 规则管理：加规则 → 列表可见 → 删除 → 清空
    PermissionStore(session.data_root).add("write_file", "src/**")
    r = _get(base, "/workspace")
    assert any(x.get("path_glob") == "src/**" for x in r["rules"])
    r = _post(base, "/permissions/delete", {"index": 0})
    assert r["ok"] and r["rules"] == []
    PermissionStore(session.data_root).add("run_command", "pytest *")
    r = _post(base, "/permissions/clear")
    assert r["ok"] and r["rules"] == []
    r = _get(base, "/workspace")
    assert r["rules"] == []


def test_approval_scope_through_http(backend):
    """/approval 回填 scope → resolve_approval 归一成 always_dir（曾经漏传 scope）。"""
    session, base = backend
    result = {}

    def worker():
        result["d"] = session.request_approval("write_file", ["/x/a.py"])

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    req = _wait_events(base, lambda ev: ev.get("type") == "approval_request", timeout=5)
    assert req
    r = _post(base, "/approval", {"id": req["id"], "decision": "always", "scope": "dir"})
    assert r["ok"]
    t.join(timeout=3)
    assert result.get("d") == "always_dir"


def test_preview_endpoints(backend):
    """POST /preview 寄存 + GET 注入收紧版 CSP；404/400/413 分支。"""
    import urllib.error

    _, base = backend
    html = ("<!doctype html><html><head><title>t</title></head>"
            "<body><button id='b'>x</button></body></html>")
    r = _post(base, "/preview", {"html": html})
    assert r["ok"] and r["url"].startswith("/preview/")
    with urllib.request.urlopen(base + r["url"], timeout=5) as resp:
        body = resp.read().decode("utf-8")
        assert resp.headers["Content-Type"].startswith("text/html")
    assert "Content-Security-Policy" in body
    assert "connect-src 'none'" in body, "必须断掉预览调后端 API 的路"
    assert "<button id='b'>x</button>" in body, "正文原样保留"
    assert body.index("Content-Security-Policy") < body.index("<title>"), "meta 应插在 head 前面"
    # 未知 id → 404
    with pytest.raises(urllib.error.HTTPError) as e1:
        urllib.request.urlopen(base + "/preview/deadbeef", timeout=5)
    assert e1.value.code == 404
    # 空 html → 400
    with pytest.raises(urllib.error.HTTPError) as e2:
        _post(base, "/preview", {"html": "   "})
    assert e2.value.code == 400


def test_preview_wrap_without_head():
    """没有 <head> 的片段：包一层壳也要带上收紧 CSP。"""
    from planner.server import _wrap_preview_html

    doc = _wrap_preview_html("<div>hi</div>")
    assert doc.startswith("<!doctype html>")
    assert "Content-Security-Policy" in doc and "<div>hi</div>" in doc
    doc2 = _wrap_preview_html("<html><body><div>y</div></body></html>")
    assert "Content-Security-Policy" in doc2 and "<div>y</div>" in doc2
