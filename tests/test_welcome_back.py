"""重启回归问候（welcome_back）测试：离线超阈值补触发、DND 跳过、状态持久化。"""

import time

import pytest

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree
from planner.session import PlannerSession


@pytest.fixture
def session(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        yield s
    finally:
        s.close()


def _trigger_calls(session, monkeypatch):
    """monkeypatch _spawn_worker 收集 trigger，避免真起 LLM 线程。"""
    calls = []
    monkeypatch.setattr(session, "_spawn_worker", lambda t: calls.append(t))
    return calls


def test_welcome_back_fires_after_long_gap(session, monkeypatch):
    calls = _trigger_calls(session, monkeypatch)
    session._last_activity_at = time.time() - 3600 * 3   # 离线 3 小时
    session._maybe_welcome_back()
    assert calls == ["welcome_back"]
    assert session.pending_response
    # 触发消息已写入 buffer
    content = "\n".join(
        m.content for m in session.recent_buffer if getattr(m, "type", "") == "human")
    assert "小时" in content


def test_welcome_back_not_fired_within_gap(session, monkeypatch):
    calls = _trigger_calls(session, monkeypatch)
    session._last_activity_at = time.time() - 600   # 10 分钟前
    session._maybe_welcome_back()
    assert calls == []


def test_welcome_back_not_fired_on_first_run(session, monkeypatch):
    calls = _trigger_calls(session, monkeypatch)
    assert session._last_activity_at == 0.0
    session._maybe_welcome_back()
    assert calls == []


def test_welcome_back_skipped_in_dnd(session, monkeypatch):
    calls = _trigger_calls(session, monkeypatch)
    monkeypatch.setattr(session, "in_dnd", lambda at=None: True)
    session._last_activity_at = time.time() - 3600 * 3
    session._maybe_welcome_back()
    assert calls == []


def test_last_activity_roundtrip(data_root):
    """buffer_state 持久化往返：last_activity_at + reminded_overdue 恢复。"""
    s1 = PlannerSession(data_root, mock=True)
    s1._last_activity_at = 12345.678
    s1._reminded_overdue = {7, 9}
    s1._save_buffer_state()
    s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        assert s2._last_activity_at == 12345.678
        assert s2._reminded_overdue == {7, 9}
    finally:
        s2.close()


def test_generation_refreshes_last_activity(session):
    before = time.time() - 9999
    session._last_activity_at = before
    session._receive("你好", trigger=True)
    session.pending_response = False
    session._generate_response("player")
    assert session._last_activity_at >= before + 1000


def test_welcome_back_restored_state_roundtrip(data_root, monkeypatch):
    """离线超阈值状态下重建 session → __init__ 自动触发回归问候。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s1 = PlannerSession(data_root, mock=True)
    s1._last_activity_at = time.time() - 3600 * 5
    s1._save_buffer_state()
    s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        assert calls == ["welcome_back"]
        content = "\n".join(
            m.content for m in s2.recent_buffer if getattr(m, "type", "") == "human")
        assert "小时" in content
    finally:
        s2.close()


def test_heartbeat_persists_across_restart(data_root, monkeypatch):
    """心跳到期时刻持久化：重启后剩余时间扣减离线时长（未到期不触发）。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.schedule_heartbeat(5, "提醒做习题")   # 5 分钟后到期
        s1._save_buffer_state()
        expected = s1._next_heartbeat_at
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)   # 模拟重启（离线几秒）
    try:
        assert calls == [], "未到期不应触发补唤醒"
        # 到期时刻恢复（未来），剩余时间 ≈ 5 分钟 - 离线时长
        assert s2._next_heartbeat_at > time.time()
        assert abs(s2._next_heartbeat_at - expected) < 3
        assert s2._heartbeat_note == "提醒做习题"
    finally:
        s2.close()


def test_heartbeat_expired_while_offline_triggers_on_start(data_root, monkeypatch):
    """离线期间心跳已到期 → 重启立即补一次自主生成。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.schedule_heartbeat(1, "测试到期")
        s1._next_heartbeat_at = time.time() - 60   # 模拟离线 1 分钟已到期
        s1._save_buffer_state()
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        assert calls == ["heartbeat"], f"到期应触发补唤醒: {calls}"
        content = "\n".join(
            m.content for m in s2.recent_buffer if getattr(m, "type", "") == "human")
        assert "过去了" in content
    finally:
        s2.close()


def test_heartbeat_expired_in_dnd_deferred(data_root, monkeypatch):
    """到期补唤醒遇免打扰 → 不触发，顺延一个正常间隔。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    monkeypatch.setattr(PlannerSession, "in_dnd", lambda self, at=None: True)
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.schedule_heartbeat(1)
        s1._next_heartbeat_at = time.time() - 60
        s1._save_buffer_state()
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        assert calls == [], "DND 不应触发补唤醒"
        assert s2._next_heartbeat_at > time.time(), "应顺延到未来"
    finally:
        s2.close()
