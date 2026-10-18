"""buffer 持久化：收消息立即落盘 + 排队消息（inbox）断点续聊。

背景（2026-10-18 事故）：外部强杀进程时，之前只在"生成结束"才保存 buffer，
用户刚发的"最后一轮消息"没落盘就丢了。修复：
1. `_receive` 收到任何消息立即 `_save_buffer_state()`（含 inbox 快照）；
2. buffer_state 新增 `inbox` 列——生成进行中排队的消息也持久化；
3. 重启恢复时把 inbox 还原并拉起循环 worker 续跑。
"""

import time

from planner.session import PlannerSession


def test_memory_tree_roundtrip_with_inbox(data_root):
    """记忆库层：inbox 与 buffer 一起存取；老库（无 inbox 列）打开时自动补列。"""
    s = PlannerSession(data_root, mock=True)
    tree = s.get_memory_tree()
    tree.save_buffer_state([{"role": "user", "content": "hi"}], 7, 3,
                           inbox=[{"role": "user", "content": "queued"}])
    state = tree.load_buffer_state()
    assert state["recent_buffer"][0]["content"] == "hi"
    assert state["inbox"][0]["content"] == "queued"


def test_receive_saves_immediately(data_root):
    """空闲时收到消息 → 立即落盘：新 session 能恢复（不再等生成结束）。"""
    s1 = PlannerSession(data_root, mock=True)
    s1._receive("重要的一句话")
    # 不调用任何"生成结束保存"路径，直接开新 session（模拟强杀后重启）
    s2 = PlannerSession(data_root, mock=True)
    texts = [str(m.content) for m in s2.recent_buffer]
    assert any("重要的一句话" in t for t in texts)


def test_queued_message_persisted_and_resumed(data_root, monkeypatch):
    """生成中收到的消息进 inbox 也落盘；重启恢复 inbox 并拉起续跑 worker。"""
    s1 = PlannerSession(data_root, mock=True)
    s1._generating = True          # 模拟"正在生成"
    s1._receive("排队中的消息")
    assert len(s1._inbox) == 1     # 确实进了队列
    s1._generating = False

    spawned = []
    monkeypatch.setattr(PlannerSession, "_player_worker",
                        lambda self: spawned.append(True))
    s2 = PlannerSession(data_root, mock=True)
    assert len(s2._inbox) == 1
    assert "排队中的消息" in str(s2._inbox[0].content)
    assert s2.pending_response is True   # 调度会接着跑
    for _ in range(100):                 # 续跑线程是异步拉起的，等一下
        if spawned:
            break
        time.sleep(0.01)
    assert spawned, "恢复 inbox 后应拉起 _player_worker 续跑"
