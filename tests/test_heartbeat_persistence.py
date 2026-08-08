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


def test_heartbeat_expired_while_offline_defers_on_start(data_root, monkeypatch):
    """离线期间心跳已到期 → 重启不立即说话，顺延到下一周期（打开时静默）。"""
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
        assert calls == [], f"启动不应补触发: {calls}"
        assert s2._next_heartbeat_at > time.time(), "到期应顺延到未来"
        content = "\n".join(
            m.content for m in s2.recent_buffer if getattr(m, "type", "") == "human")
        assert "定时任务到点" not in content, "启动不应生成任何内容"
    finally:
        s2.close()


def test_heartbeat_expired_in_dnd_deferred(data_root, monkeypatch):
    """运行中心跳到期遇免打扰 → 不触发，顺延（启动补唤醒不受 DND 限制）。"""
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    monkeypatch.setattr(PlannerSession, "in_dnd", lambda self, at=None: True)
    s = PlannerSession(data_root, mock=True)
    try:
        s.schedule_heartbeat(1)
        s._next_heartbeat_at = time.time() - 60
        s._fire_heartbeat()                 # 运行中心跳：DND 顺延
        assert calls == [], "DND 时运行中心跳不应触发"
        assert s._next_heartbeat_at > time.time(), "应顺延到未来"
    finally:
        s.close()


def test_startup_grace_window(data_root):
    """启动宽限期：打开后 STARTUP_GRACE_SECONDS 内不自动说话（_in_startup_grace）。"""
    from planner.session import STARTUP_GRACE_SECONDS
    s = PlannerSession(data_root, mock=True)
    try:
        assert s._in_startup_grace() is True, "刚启动应在宽限期内"
        s._started_at = time.time() - (STARTUP_GRACE_SECONDS + 1)
        assert s._in_startup_grace() is False, "宽限期过后应解除"
    finally:
        s.close()


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


def test_startup_defer_keeps_original_interval(data_root, monkeypatch):
    """启动顺延沿用原心跳间隔（而非固定 60 分钟）。

    回归：短心跳（如 15 分钟）离线过期后重开，顺延应为原间隔，
    否则显示"60 分钟后醒来"造成重置感。
    """
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.schedule_heartbeat(15, "十五分钟节奏")      # 原间隔 15 分钟
        s1._next_heartbeat_at = time.time() - 60     # 已过期
        s1._save_buffer_state()
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)        # 重启 → 顺延（不触发）
    try:
        assert calls == [], "启动不应触发"
        # 顺延沿用原间隔：next ≈ now + 15 分钟（而非 60 分钟）
        remain = s2._next_heartbeat_at - time.time()
        assert 800 < remain < 1000, f"顺延应≈原间隔 15 分钟，实际 {remain:.0f} 秒"
    finally:
        s2.close()


def test_fallback_persisted_immediately(data_root, monkeypatch):
    """启动顺延的保底立即落盘：退出/中断后重开恢复新计时，不再重复顺延。

    回归：killBackend 退出不保存，若保底未落盘，重开恢复旧过期值 →
    每次都重新顺延、显示固定 59 分钟（"重置感"）。
    """
    calls = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls.append(t))
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.schedule_heartbeat(1)
        s1._next_heartbeat_at = time.time() - 60     # 已过期
        s1._check_startup_heartbeat()                # 启动检查 → 顺延并立即保存
        saved_next = s1._next_heartbeat_at
        assert saved_next > time.time()
    finally:
        s1.close()                                   # 模拟退出（不额外保存）

    calls2 = []
    monkeypatch.setattr(PlannerSession, "_spawn_worker", lambda self, t: calls2.append(t))
    s2 = PlannerSession(data_root, mock=True)        # 重开
    try:
        assert calls2 == [], "保底已落盘 → 重开恢复新计时，不应再次顺延"
        assert abs(s2._next_heartbeat_at - saved_next) < 5, "恢复的是新保底时刻"
    finally:
        s2.close()
