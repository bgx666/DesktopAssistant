"""撤销（undo）测试：删除该消息及其后的对话；已压缩的消息无法撤销。"""

import time

from langchain_core.messages import HumanMessage

from planner.session import PlannerSession


def _drive_generation(session, content: str) -> None:
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response("player")


def test_undo_removes_message_and_after(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        msg_id = s.enqueue_player_message("帮我安排学习计划")
        # 等生成结束（mock 快，同步轮询 buffer 出现 ai 消息）
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(getattr(m, "type", "") == "ai" for m in s.recent_buffer):
                break
            time.sleep(0.1)
        total_before = len(s.recent_buffer)
        assert total_before > 1

        r = s.undo_message(msg_id)
        assert r["ok"] is True
        assert r["removed"] == total_before   # 该消息及其后全部删除
        assert len(s.recent_buffer) == 0
    finally:
        s.close()


def test_undo_keeps_earlier_messages(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        # 手动塞一条"更早"的消息（模拟之前的上下文）
        s._receive("更早的对话", trigger=False)
        early_id = next(m.id for m in s.recent_buffer
                        if "更早的对话" in str(m.content))

        s.enqueue_player_message("第一句话")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(getattr(m, "type", "") == "ai" for m in s.recent_buffer):
                break
            time.sleep(0.1)
        first_id = next(m.id for m in s.recent_buffer
                        if getattr(m, "type", "") == "human" and "第一句话" in str(m.content))
        s.enqueue_player_message("第二句话")
        deadline = time.time() + 15
        while time.time() < deadline:
            if sum(1 for m in s.recent_buffer if "第二句话" in str(getattr(m, "content", ""))):
                break
            time.sleep(0.1)

        before = len(s.recent_buffer)
        r = s.undo_message(first_id)
        assert r["ok"] is True
        assert len(s.recent_buffer) < before
        # 撤销 = 删除该消息及其之后（含该条本身）：第一/二句都没了
        assert not any("第一句话" in str(m.content) for m in s.recent_buffer)
        assert not any("第二句话" in str(m.content) for m in s.recent_buffer)
        # 更早的上下文保留
        assert any(m.id == early_id for m in s.recent_buffer)
    finally:
        s.close()


def test_undo_unknown_message_compressed(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        s.recent_buffer.append(HumanMessage(content="占位"))
        r = s.undo_message("nonexistent-id-123")
        assert r["ok"] is False
        assert r["reason"] == "compressed"
        assert len(s.recent_buffer) == 1   # 未删除任何消息
    finally:
        s.close()


def test_undo_roundtrip_persisted(data_root):
    """撤销后 buffer_state 持久化：重建 session 时上下文已截断。"""
    s1 = PlannerSession(data_root, mock=True)
    try:
        msg_id = s1.enqueue_player_message("要被撤销的话")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(getattr(m, "type", "") == "ai" for m in s1.recent_buffer):
                break
            time.sleep(0.1)
        before = len(s1.recent_buffer)
        r = s1.undo_message(msg_id)
        assert r["ok"] is True
        assert len(s1.recent_buffer) == 0
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        assert len(s2.recent_buffer) == 0
        assert s2._load_buffer_state() is False or len(s2.recent_buffer) == 0
    finally:
        s2.close()
