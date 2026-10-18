"""思考流（reasoning_content）：langchain 透传 shim + session thinking_stream 事件。

- 假端点回归：langchain_openai 的流式转换要保留 reasoning_content（shim 生效），
  且**不把 reasoning 回传给端点**（DeepSeek 要求 history 不带 reasoning）；
- session：mock「多想」触发 → 逐块推 thinking_stream；buffer 不落 reasoning。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import planner.llm  # noqa: E402,F401  先导入：安装 reasoning passthrough shim
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from planner.session import PlannerSession  # noqa: E402

_SSE_CHUNKS = [
    {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
     "choices": [{"index": 0, "delta": {"role": "assistant",
                                        "reasoning_content": "先想一下："},
                  "finish_reason": None}]},
    {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
     "choices": [{"index": 0, "delta": {"reasoning_content": "再想一步。"},
                  "finish_reason": None}]},
    {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
     "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
    {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
     "choices": [],
     "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280,
               "prompt_cache_hit_tokens": 1024, "prompt_cache_miss_tokens": 176}},
]


class _FakeEndpoint(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):   # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in _SSE_CHUNKS:
            self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_endpoint():
    _FakeEndpoint.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEndpoint)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_reasoning_content_passthrough(fake_endpoint):
    """shim 生效：流式 chunk 的 reasoning_content 进 additional_kwargs。"""
    model = ChatOpenAI(model="m", api_key="x", base_url=fake_endpoint)
    chunks = list(model.stream([HumanMessage("hi")]))
    reasons = "".join(
        str((c.additional_kwargs or {}).get("reasoning_content") or "") for c in chunks)
    texts = "".join(str(c.content or "") for c in chunks)
    assert reasons == "先想一下：再想一步。"
    assert "你好" in texts


def test_reasoning_never_sent_back_to_endpoint(fake_endpoint):
    """带 reasoning 的历史消息回传时，请求体里不能出现 reasoning_content。"""
    model = ChatOpenAI(model="m", api_key="x", base_url=fake_endpoint)
    history = [
        HumanMessage("hi"),
        AIMessage(content="答", additional_kwargs={"reasoning_content": "内部推理"}),
    ]
    list(model.stream(history))
    assert _FakeEndpoint.requests, "假端点应收到请求"
    sent = json.dumps(_FakeEndpoint.requests[-1], ensure_ascii=False)
    assert "内部推理" not in sent
    assert "reasoning_content" not in sent


def _drive_generation(session, content: str) -> None:
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response("player")


def test_mock_reasoning_emits_thinking_stream(data_root):
    """mock「多想」触发：逐块推 thinking_stream；思考**随消息持久化**（历史可见）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "多想一下这个怎么安排")
        events = s.drain_events()
        thinks = [e for e in events if e["type"] == "thinking_stream"]
        joined = "".join(str(e["content"]) for e in thinks)
        assert thinks and "mock 思考流" in joined
        # 同一轮共享一个 msg_id（前端按 id 归块）
        assert len({e["msg_id"] for e in thinks}) == 1
        # 顺序：思考流在正文流之前
        kinds = [e["type"] for e in events]
        assert kinds.index("thinking_stream") < kinds.index("text_stream")
        # 思考正文不混进消息 content（只走 additional_kwargs）
        assert not any("mock 思考流" in str(getattr(m, "content", "") or "")
                       for m in s.recent_buffer)
        # 持久化：AI 消息的 additional_kwargs 带着完整思考（供 /history 重建）
        persisted = "".join(
            str((getattr(m, "additional_kwargs", None) or {}).get("reasoning_content") or "")
            for m in s.recent_buffer)
        assert "mock 思考流" in persisted
    finally:
        s.close()


def test_real_stream_path_persists_reasoning(data_root, fake_endpoint):
    """**真实流式链路**（非 mock）：chunk 聚合后的 AI 消息带完整 reasoning，
    且 usage（缓存命中）走到统计——用户实机（OpenCode Go 流式）走的就是这条路径。"""
    s = PlannerSession(data_root, mock=False)
    try:
        s.settings["llm_api_key_deepseek"] = "x"
        s.settings["llm_base_url_deepseek"] = fake_endpoint
        s.settings["llm_model_deepseek"] = "m"
        _drive_generation(s, "你好")
        persisted = "".join(
            str((getattr(m, "additional_kwargs", None) or {}).get("reasoning_content") or "")
            for m in s.recent_buffer)
        assert "先想一下：再想一步。" in persisted, \
            "流式 chunk 聚合后 reasoning 应完整落进消息（历史可见）"
        recs = s.llm_stats.list()
        assert recs and recs[-1]["cache_known"] and recs[-1]["hit"] == 1024
    finally:
        s.close()


def test_reasoning_survives_restart(data_root):
    """buffer_state 持久化往返：重启（新 session）后思考仍在 buffer 里（历史可见）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "多想一下")
    finally:
        s.close()
    s2 = PlannerSession(data_root, mock=True)
    try:
        persisted = "".join(
            str((getattr(m, "additional_kwargs", None) or {}).get("reasoning_content") or "")
            for m in s2.recent_buffer)
        assert "mock 思考流" in persisted, "重启后思考应随 buffer 恢复"
    finally:
        s2.close()


def test_history_returns_thinking_entries(data_root):
    """/history 对带思考的 AI 消息返回独立 thinking 条目（放在 assistant 正文之前）。"""
    import json as _json
    import urllib.request

    from planner.server import create_server

    session = PlannerSession(data_root, mock=True)
    httpd = create_server(session, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _drive_generation(session, "多想一下")
        with urllib.request.urlopen(base + "/history", timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8"))
        roles = [m["role"] for m in data["messages"]]
        assert "thinking" in roles
        t_idx = roles.index("thinking")
        think = data["messages"][t_idx]
        assert "mock 思考流" in think["content"]
        # thinking 在紧随其后的 assistant 正文之前
        after = [m for m in data["messages"][t_idx + 1:] if m["role"] == "assistant"]
        assert after, "思考条目后应有 assistant 正文"
    finally:
        httpd.shutdown()
        session.close()


def test_no_thinking_stream_without_trigger(data_root):
    """普通消息（mock 无脚本触发）不产生 thinking_stream 事件。"""
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "帮我安排一下学习计划")
        events = s.drain_events()
        assert not [e for e in events if e["type"] == "thinking_stream"]
    finally:
        s.close()
