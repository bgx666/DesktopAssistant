"""心跳持久化（跨重启剩余时间扣减 / 到期启动补唤醒）测试。

2026-10-19 起心跳是"用户授权的定时器"：醒来即消费（next=0），**不自动重排**、
不重试——是否再醒由模型决定（巡检循环里由模型显式调 heartbeat）。
"""

import time

from planner.session import PlannerSession


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
        assert s2._next_heartbeat_at > time.time()
        assert abs(s2._next_heartbeat_at - expected) < 3
        assert s2._heartbeat_note == "提醒做习题"
    finally:
        s2.close()


def test_heartbeat_expired_while_offline_triggers_on_start(data_root, monkeypatch):
    """离线期间心跳已到期 → 重启立即补触发一次；醒来即消费（next=0，不自动重排）。"""
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
        assert calls == ["heartbeat"], f"启动应补触发: {calls}"
        assert s2._next_heartbeat_at == 0, "醒来即消费：不自动重排"
        content = "\n".join(
            m.content for m in s2.recent_buffer if getattr(m, "type", "") == "human")
        assert "定时唤醒到点" in content, "启动应注入心跳消息"
    finally:
        s2.close()


def test_startup_grace_window(data_root):
    """启动宽限期：打开后 STARTUP_GRACE_SECONDS 内不触发定时提醒。"""
    from planner.session import STARTUP_GRACE_SECONDS
    s = PlannerSession(data_root, mock=True)
    try:
        assert s._in_startup_grace() is True, "刚启动应在宽限期内"
        s._started_at = time.time() - (STARTUP_GRACE_SECONDS + 1)
        assert s._in_startup_grace() is False, "宽限期过后应解除"
    finally:
        s.close()


def test_fire_heartbeat_consumes_and_does_not_reschedule(data_root, monkeypatch):
    """心跳触发即消费：next=0、立即落盘；重启后不会重复触发（不自动重排/不重试）。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s = PlannerSession(data_root, mock=True)
    try:
        s.schedule_heartbeat(1)
        s._next_heartbeat_at = time.time() - 5
        s._fire_heartbeat()
        assert calls == ["heartbeat"], "心跳应触发"
        assert s._next_heartbeat_at == 0, "醒来即消费，不自动重排"
    finally:
        s.close()

    calls2 = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls2.append(t))
    s2 = PlannerSession(data_root, mock=True)   # 模拟重启
    try:
        assert calls2 == [], "消费已落盘 → 重启不重复补触发"
        assert s2._next_heartbeat_at == 0
    finally:
        s2.close()


def test_startup_trigger_consumes_immediately(data_root, monkeypatch):
    """启动补触发后立即落盘 next=0：退出/中断后重开不重复补触发。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.schedule_heartbeat(1)
        s1._next_heartbeat_at = time.time() - 60
        s1._check_startup_heartbeat()
        assert calls == ["heartbeat"], f"启动应补触发: {calls}"
        assert s1._next_heartbeat_at == 0
    finally:
        s1.close()

    calls2 = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls2.append(t))
    s2 = PlannerSession(data_root, mock=True)
    try:
        assert calls2 == [], "已消费并落盘 → 重开不应再次补触发"
        assert s2._next_heartbeat_at == 0
    finally:
        s2.close()
