"""心跳持久化（跨重启剩余时间扣减 / 到期启动补唤醒 / DND 顺延）测试。"""

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
        assert "心跳到了" in content, "补唤醒文案应是指令式而非角色扮演"
        assert "你醒" not in content, "不应出现'你醒了'这类角色扮演文案"
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


def test_fire_heartbeat_leaves_scheduled_next(data_root, monkeypatch):
    """心跳触发后 next 不为 0（先落保底调度）：触发瞬间退出也不会落盘 0。

    回归：旧实现触发时清零 next 后异步生成，若进程在生成前退出，
    落盘 next=0 → 重启后心跳被重置为默认 30 分钟。
    """
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s = PlannerSession(data_root, mock=True)
    try:
        s.schedule_heartbeat(1)
        s._next_heartbeat_at = time.time() - 5   # 模拟到期
        s._fire_heartbeat()
        assert calls == ["heartbeat"], "心跳应触发"
        assert s._next_heartbeat_at > time.time(), "触发后应存在保底调度"
        s._save_buffer_state()   # 模拟触发瞬间退出前的保存
    finally:
        s.close()

    s2 = PlannerSession(data_root, mock=True)   # 模拟重启
    try:
        assert s2._next_heartbeat_at > time.time(), "重启后 next 不应为 0（不应重置为默认 30 分钟）"
    finally:
        s2.close()
