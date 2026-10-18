"""用量/缓存命中统计：usage 透传 shim + 存储聚合 + session 记录 + /llm/stats 端点。"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import planner.llm  # noqa: E402,F401  先导入：安装 usage/reasoning shim
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from planner.llm_stats import LlmStats  # noqa: E402
from planner.session import PlannerSession  # noqa: E402

_USAGE_DEEPSEEK = {
    "prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280,
    "prompt_cache_hit_tokens": 1024, "prompt_cache_miss_tokens": 176,
}
_CHUNK_MAIN = {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
               "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]}


class _UsageEndpoint(BaseHTTPRequestHandler):
    def do_POST(self):   # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in (_CHUNK_MAIN, {**_CHUNK_MAIN, "choices": [], "usage": _USAGE_DEEPSEEK}):
            self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_endpoint():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _UsageEndpoint)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_deepseek_cache_hit_mapped_to_usage_metadata(fake_endpoint):
    """DeepSeek 顶层 prompt_cache_hit_tokens → usage_metadata.cache_read（shim）。"""
    model = ChatOpenAI(model="m", api_key="x", base_url=fake_endpoint, stream_usage=True)
    usage = None
    for ch in model.stream([HumanMessage("hi")]):
        if ch.usage_metadata:
            usage = ch.usage_metadata
    assert usage, "流式最终块应带 usage_metadata"
    assert usage["input_tokens"] == 1200 and usage["output_tokens"] == 80
    assert usage["input_token_details"].get("cache_read") == 1024


def test_stats_store_ring_and_summary(tmp_path):
    stats = LlmStats(tmp_path, max_records=3)
    for i in range(5):
        stats.record(input_tokens=1000, output_tokens=100, cache_hit=800,
                     cache_known=True, reason_tokens=60, duration_s=1.0)
    assert len(stats.list()) == 3, "环形窗口只保留最近 max_records 条"
    s = stats.summary()
    assert s["count"] == 3 and s["cache_rate"] == 80.0
    assert s["avg_tps"] == 100.0 and s["avg_in"] == 1000 and s["avg_out"] == 100
    assert s["avg_reason"] == 60
    assert stats.list()[-1]["reason"] == 60
    # 重新加载（持久化）
    stats2 = LlmStats(tmp_path, max_records=3)
    assert stats2.summary()["count"] == 3
    # 未知缓存字段的记录不计入命中率
    stats2.record(input_tokens=500, output_tokens=50, cache_known=False, duration_s=0.5)
    s2 = stats2.summary()
    assert s2["cache_rate"] == 80.0, "cache_known=False 的轮次不进命中率分母"
    # 全部未知 → cache_rate None（显示"端点未报"）
    stats3 = LlmStats(tmp_path / "sub", max_records=3)
    stats3.record(input_tokens=10, output_tokens=1)
    assert stats3.summary()["cache_rate"] is None


def _drive_generation(session, content: str) -> None:
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response("player")


def test_session_records_stats_and_pushes_event(data_root):
    """mock 每次生成带确定性 usage（1200/900）→ 记录 + stats_update 事件。"""
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "帮我安排一下学习计划")
        recs = s.llm_stats.list()
        assert recs, "生成后应有统计记录"
        last = recs[-1]
        assert last["in"] > 0 and last["out"] > 0
        assert last["cache_known"] is True and last["hit"] > 0
        assert last["rounds"] >= 1
        assert last["mock"] is True
        assert "reason" in last                      # 思考 token 字段（mock 为 0）
        # 标题栏实报输入：mock 每次调用 input_tokens=1200 → 最近一次实报=1200
        assert s.context_stats()["real_tokens"] == 1200
        events = s.drain_events()
        assert any(e["type"] == "stats_update" for e in events)
        summary = s.llm_stats.summary()
        assert summary["count"] == len(recs)
        assert summary["cache_rate"] == 75.0   # mock 固定 900/1200
    finally:
        s.close()


def test_llm_stats_endpoint(data_root):
    from planner.server import create_server
    import urllib.request

    session = PlannerSession(data_root, mock=True)
    httpd = create_server(session, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # 先跑一轮生成 → 有数据
        _drive_generation(session, "帮我安排一下学习计划")
        deadline = time.time() + 5
        payload = None
        while time.time() < deadline:
            with urllib.request.urlopen(base + "/llm/stats", timeout=5) as r:
                payload = json.loads(r.read().decode("utf-8"))
            if payload["summary"]["count"]:
                break
            time.sleep(0.1)
        assert payload["ok"] and payload["summary"]["count"] >= 1
        assert payload["records"], "应有明细记录"
        rec = payload["records"][-1]
        assert {"ts", "in", "out", "hit", "tps", "cache_known"} <= set(rec)
    finally:
        httpd.shutdown()
        session.close()
