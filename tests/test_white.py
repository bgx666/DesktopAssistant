"""White 子代理：工具白名单/硬隔离、异步结果、上下文复用与关闭。"""

import time

from planner.agent_context import _current, set_current_agent
from planner.middleware import ApprovalMiddleware
from planner.session import PlannerSession
from planner.white_agent import WhiteAgent


def _wait_idle(white, timeout: float = 10.0):
    end = time.time() + timeout
    while time.time() < end and white.status == "running":
        time.sleep(0.05)
    return white


def test_white_tools_confined(data_root, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(ws)})
        w = WhiteAgent(s, notify=False)
        names = {t.name for t in w._build_tools()}
        assert "finish_task" in names
        assert "web_search" in names and "fetch_web" in names
        assert "read_file" in names and "write_file" in names
        # MVP：不能执行命令、不能展示图片、不能碰日历/记忆/心跳、不能递归委派
        for banned in ("run_command", "show_image", "heartbeat", "ask_user",
                       "list_schedule", "explore_memory_tree", "white_task",
                       "capture_screen", "view_image", "render_page"):
            assert banned not in names, banned
    finally:
        s.close()


def test_white_no_workspace_has_no_file_tools(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        w = WhiteAgent(s, notify=False)
        names = {t.name for t in w._build_tools()}
        assert "web_search" in names and "finish_task" in names
        assert "read_file" not in names and "write_file" not in names
    finally:
        s.close()


def test_white_outside_workspace_is_hard_denied(data_root, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(ws)})
        mw = ApprovalMiddleware(s)
        token = set_current_agent("white")
        try:
            decision, _ = mw._authorize("read_file", {"path": str(outside)})
            assert decision == "deny"
            decision2, _ = mw._authorize("read_file", {"path": str(ws / "a.txt")})
            assert decision2 == "allow"
        finally:
            _current.reset(token)
    finally:
        s.close()


def test_white_wrapped_read_file_blocks_relative_traversal(data_root, tmp_path):
    """White 的 read_file 包装器：相对路径也硬限制在工作区内。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("inside-ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside-secret", encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(ws)})
        w = WhiteAgent(s, notify=False)
        tools = {t.name: t for t in w._build_tools()}
        assert "已拒绝" in tools["read_file"].invoke({"path": "../secret.txt"})
        assert "已拒绝" in tools["list_dir"].invoke({"path": ".."})
        assert "inside-ok" in tools["read_file"].invoke({"path": "a.txt"})
    finally:
        s.close()


def test_white_no_workspace_denies_files_even_with_agent(data_root, tmp_path):
    s = PlannerSession(data_root, mock=True)
    try:
        mw = ApprovalMiddleware(s)
        token = set_current_agent("white")
        try:
            decision, _ = mw._authorize("read_file", {"path": str(tmp_path / "x.txt")})
            assert decision == "deny"
        finally:
            _current.reset(token)
    finally:
        s.close()


def test_white_dispatch_result_reuse_and_close(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"white_enabled": True, "white_max_steps": 5})
        w = WhiteAgent(s, notify=False)
        r = w.dispatch("白色任务：检查工作区", reuse=False)
        assert r["ok"] and not r["queued"]
        _wait_idle(w)
        assert w.status == "idle"
        assert w.result and w.result["status"] == "done"
        assert "White 测试任务完成" in w.result["summary"]
        assert w.history()[-1]["role"] == "result"

        # 相关后续任务：复用上下文
        before = len(w.messages)
        w.dispatch("白色任务：再补充一点", reuse=True)
        _wait_idle(w)
        assert len(w.messages) > before

        # 全新任务：丢弃旧上下文
        w.dispatch("白色任务：换一个全新任务", reuse=False)
        _wait_idle(w)
        instructions = [str(m.content) for m in w.messages
                        if (getattr(m, "metadata", None) or {}).get("white_instruction")]
        assert instructions and "换一个全新任务" in instructions[-1]
        assert not any("检查工作区" in x for x in instructions)

        assert w.close()["ok"]
        assert w.status == "closed"
    finally:
        s.close()


def test_white_queue_when_running(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"white_enabled": True, "white_max_steps": 5})
        w = WhiteAgent(s, notify=False)
        w.dispatch("白色任务：第一条", reuse=False)
        r = w.dispatch("白色任务：第二条", reuse=True)
        assert r["ok"]
        _wait_idle(w)
        # 队列里的第二条会被执行（示例 mock 结果一样，但指令会进历史）
        texts = [str(m.content) for m in w.messages]
        assert any("第二条" in t for t in texts)
    finally:
        s.close()

def test_white_stop_aborts_blocked_model_immediately(data_root):
    """White 停止同样要立即中断在途模型请求，并保留已有上下文。"""
    import threading
    from typing import ClassVar

    from langchain_core.language_models.chat_models import BaseChatModel

    release = threading.Event()
    started = threading.Event()
    closed = threading.Event()

    class _Root:
        def close(self):
            closed.set()
            release.set()

    class _BlockedModel(BaseChatModel):
        root_client: ClassVar = _Root()

        def _llm_type(self):
            return "blocked"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            release.wait(20)
            raise RuntimeError("aborted")

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            release.wait(20)
            raise RuntimeError("aborted")
            yield  # pragma: no cover

    s = PlannerSession(data_root, mock=True)
    try:
        w = WhiteAgent(s, model=_BlockedModel(), notify=False)
        assert w.dispatch("白色停止测试", reuse=False)["ok"]
        assert started.wait(5), "White 模型应已开始调用"
        t0 = time.time()
        r = w.stop()
        stop_elapsed = time.time() - t0
        end = time.time() + 3
        while w.status == "running" and time.time() < end:
            time.sleep(0.02)
        assert r["ok"] and stop_elapsed < 1.0
        assert closed.is_set(), "White 停止应关闭自己的模型客户端"
        assert w.status == "idle", w.status
        assert w.result and w.result["status"] == "blocked"
        assert any("白色停止测试" in str(m.content) for m in w.messages), "已有上下文应保留"
    finally:
        s.close()
