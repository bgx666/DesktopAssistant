"""ask_user 结构化提问：挂起/回填/超时/停止 + 工具与 HTTP 端点集成。

全部 mock LLM + tmp_path 隔离，不触碰真实 data/。
"""

from __future__ import annotations

import threading
import time

import pytest

from planner.session import PlannerSession


@pytest.fixture
def session(data_root):
    s = PlannerSession(data_root, mock=True)
    yield s
    s.close()


def _wait_pending(session, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session.buffer_lock:
            if session.pending_questions:
                return next(iter(session.pending_questions))
        time.sleep(0.02)
    return None


def _tools(session):
    from planner.tools import build_tools
    return {t.name: t for t in build_tools(session)}


def test_ask_user_blocks_and_returns_selection(session):
    result = {}
    t = threading.Thread(target=lambda: result.update(
        r=session.ask_user("这两个方案先做哪个？", ["A：先做方案一", "B：先做方案二"])))
    t.start()
    qid = _wait_pending(session)
    assert qid
    assert session.resolve_question(qid, selected="B：先做方案二") is True
    t.join(timeout=3)
    assert "用户选择了「B：先做方案二」" in result["r"]
    events = session.drain_events()
    assert any(ev["type"] == "question_request" and ev["id"] == qid for ev in events)
    assert any(ev["type"] == "question_resolved" and ev["id"] == qid for ev in events)


def test_ask_user_free_text_and_mixed(session):
    # 仅自由文本
    result = {}
    t = threading.Thread(target=lambda: result.update(r=session.ask_user("用哪个名字？")))
    t.start()
    qid = _wait_pending(session)
    assert session.resolve_question(qid, answer="叫 xiaozhu 吧") is True
    t.join(timeout=3)
    assert result["r"] == "用户回答：叫 xiaozhu 吧"
    # 选项 + 补充说明
    result2 = {}
    t2 = threading.Thread(target=lambda: result2.update(r=session.ask_user("选哪个？", ["A", "B"])))
    t2.start()
    qid2 = _wait_pending(session)
    session.resolve_question(qid2, answer="B 但先确认一下", selected="B")
    t2.join(timeout=3)
    assert "用户选择了「B」" in result2["r"] and "B 但先确认一下" in result2["r"]


def test_ask_user_timeout(session):
    session.settings["approval_timeout_seconds"] = 0.3
    t0 = time.time()
    r = session.ask_user("在吗？")
    assert "超时" in r and time.time() - t0 < 3


def test_ask_user_stop_wakes(session):
    result = {}
    t = threading.Thread(target=lambda: result.update(r=session.ask_user("等你拍板")))
    t.start()
    qid = _wait_pending(session)
    assert qid
    session._stop_requested = True
    t.join(timeout=3)
    assert "停止" in result["r"]


def test_ask_user_tool_normalizes_and_validates(session):
    tools = _tools(session)
    assert "ask_user" in tools
    # 空问题 → 直接报错字符串（不挂起）
    assert "question 不能为空" in tools["ask_user"].invoke({"question": "  ", "options": []})
    # options 清理：空白丢弃、超 4 个截断（在挂起里检查收到的 options）
    result = {}
    t = threading.Thread(target=lambda: result.update(
        r=tools["ask_user"].invoke({"question": "选一个", "options": [" ", "A", "B", "C", "D", "E"]})))
    t.start()
    qid = _wait_pending(session)
    with session.buffer_lock:
        entry = session.pending_questions[qid]
    assert entry["options"] == ["A", "B", "C", "D"]
    session.resolve_question(qid, selected="A")
    t.join(timeout=3)
    assert "用户选择了「A」" in result["r"]


def test_question_endpoint(data_root):
    """HTTP 回填：/dequeue 能看到 question_request，POST /question 后工具结果进对话。"""
    import json as _json
    import urllib.error
    import urllib.request

    from planner.server import create_server

    session = PlannerSession(data_root, mock=True)
    httpd = create_server(session, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _question_endpoint_flow(session, base)
    finally:
        httpd.shutdown()
        session.close()


def _question_endpoint_flow(session, base):
    import json as _json
    import urllib.request

    result = {}

    def worker():
        result.update(r=session.ask_user("服务器上的问题", ["是", "否"]))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    req_ev = None
    deadline = time.time() + 5
    while time.time() < deadline and req_ev is None:
        with urllib.request.urlopen(base + "/dequeue", timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8"))
        for ev in data["events"]:
            if ev["type"] == "question_request":
                req_ev = ev
        time.sleep(0.05)
    assert req_ev and req_ev["question"] == "服务器上的问题"

    req = urllib.request.Request(
        base + "/question",
        data=_json.dumps({"id": req_ev["id"], "selected": "是"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert _json.loads(r.read().decode("utf-8"))["ok"] is True
    t.join(timeout=3)
    assert "用户选择了「是」" in result["r"]

    # 空回填 → 400
    req2 = urllib.request.Request(
        base + "/question",
        data=_json.dumps({"id": req_ev["id"]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req2, timeout=5)
    assert exc.value.code == 400
