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


def test_toggle_mock(backend):
    _, base = backend
    r = _post(base, "/toggle_mock")
    assert r["mode"] == "llm"
    r2 = _post(base, "/toggle_mock")
    assert r2["mode"] == "mock"
