"""节点覆盖时间范围（time_start/time_end）测试：
消息 metadata.ts 打点、叶子压缩取首末消息时间、向上压缩聚合子节点时间。"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree
from planner.session import PlannerSession


def test_receive_stamps_ts(data_root):
    """_receive 的消息带 metadata.ts（秒级 ISO）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        m = s._receive("你好", trigger=False)
        ts = (getattr(m, "metadata", None) or {}).get("ts")
        assert ts and len(ts) == 19, f"ts 应为 'YYYY-MM-DD HH:MM:SS': {ts!r}"
    finally:
        s.close()


def test_stamp_missing_ts_fills_buffer(data_root):
    """buffer 中缺 ts 的 ai/tool 消息被补打（回写后调用）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.recent_buffer = [
            HumanMessage(content="用户", id="h1"),
            AIMessage(content="回复", id="a1"),
            ToolMessage(content="结果", tool_call_id="t1", id="t2"),
        ]
        s._stamp_missing_ts()
        for m in s.recent_buffer:
            assert (getattr(m, "metadata", None) or {}).get("ts"), "每条消息都应有 ts"
    finally:
        s.close()


def test_leaf_time_range_from_batch(data_root):
    """叶子节点 time_start/end = 被压缩消息首末 ts。"""
    tree = SQLiteMemoryTree("assistant", data_root / "mt.db")
    try:
        nid = tree.add_leaf(
            "摘要",
            (0, 39),
            None,
            details=[],
            time_range=("2026-08-08 10:00:00", "2026-08-08 12:30:00"),
        )
        info = tree.get_node_children_info(nid)
        assert info["time_start"] == "2026-08-08 10:00:00"
        assert info["time_end"] == "2026-08-08 12:30:00"
    finally:
        tree.close()


def test_compact_aggregates_child_times(data_root):
    """父节点时间 = 子节点 time_start 最小 / time_end 最大。"""
    tree = SQLiteMemoryTree("assistant", data_root / "mt.db")
    try:
        ids = []
        for i, (ts, te) in enumerate([
                ("2026-08-01 09:00:00", "2026-08-01 11:00:00"),
                ("2026-08-02 10:00:00", "2026-08-02 14:00:00"),
                ("2026-08-03 08:00:00", "2026-08-03 20:00:00")]):
            ids.append(tree.add_leaf(f"摘要{i}", (i * 40, i * 40 + 39), None,
                                     time_range=(ts, te)))
        pid = tree.compact(ids, "父摘要")
        info = tree.get_node_children_info(pid)
        assert info["time_start"] == "2026-08-01 09:00:00"
        assert info["time_end"] == "2026-08-03 20:00:00"
        # 子节点转为非活跃
        nodes = tree.get_nodes_at_level(0)
        assert len(nodes) == 0
    finally:
        tree.close()


def test_compact_ignores_missing_times(data_root):
    """部分子节点无时间 → 仅聚合有时间的；全无 → None。"""
    tree = SQLiteMemoryTree("assistant", data_root / "mt.db")
    try:
        a = tree.add_leaf("摘要A", (0, 39), None, time_range=("2026-08-01 09:00:00", "2026-08-01 11:00:00"))
        b = tree.add_leaf("摘要B", (40, 79), None)          # 无时间（旧数据）
        pid = tree.compact([a, b], "父摘要")
        info = tree.get_node_children_info(pid)
        assert info["time_start"] == "2026-08-01 09:00:00"
        assert info["time_end"] == "2026-08-01 11:00:00"
        # 全无时间
        c = tree.add_leaf("摘要C", (80, 119), None)
        d = tree.add_leaf("摘要D", (120, 159), None)
        pid2 = tree.compact([c, d], "父摘要2")
        info2 = tree.get_node_children_info(pid2)
        assert info2["time_start"] is None and info2["time_end"] is None
    finally:
        tree.close()


def test_compress_snapshot_stores_time_range(data_root, monkeypatch):
    """完整压缩链路：带 ts 的消息压缩后，叶子节点时间 = batch 首末消息 ts。"""
    from planner.middleware import SummarizationMiddleware, MemoryNodeOutput

    s = PlannerSession(data_root, mock=True)
    try:
        tree = s.get_memory_tree()
        # 构造带时间戳的原始消息（60 条，跨时间段）
        import datetime
        base = datetime.datetime(2026, 8, 8, 9, 0, 0)
        msgs = []
        for i in range(60):
            ts = (base + datetime.timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
            msgs.append(HumanMessage(content=f"消息{i}", metadata={"ts": ts}))
        comp = SummarizationMiddleware(s)
        orig_call = comp._call_compress

        def fake_call(ctx, instruction):
            return MemoryNodeOutput(
                summary="压缩摘要",
                profile={"preferences": [], "personality": [], "habits": [], "goals": []},
                future_notes=[],
            )

        monkeypatch.setattr(comp, "_call_compress", fake_call)
        removes, adds = comp.compress_snapshot(msgs)
        assert removes and adds, "应产生压缩"
        nodes = tree.get_nodes_at_level(0)
        assert len(nodes) == 1
        assert nodes[0]["time_start"] == "2026-08-08 09:00:00"
        assert nodes[0]["time_end"] == "2026-08-08 09:39:00"   # 最早的 40 条（09:00~09:39）
        # 节点文本里显示时间范围
        node_text = adds[0].content
        assert "时间]" in node_text, "节点文本应显示时间范围"
    finally:
        s.close()


def test_render_time_range_format():
    from planner.middleware import SummarizationMiddleware as SM
    # 同日：开始带年份，结束省略日期
    assert SM._fmt_time_range("2026-08-08 10:00:00", "2026-08-08 12:30:00") == "2026-08-08 10:00 ~ 12:30"
    # 跨日：结束带完整日期
    assert SM._fmt_time_range("2026-08-08 22:00:00", "2026-08-09 01:00:00") == "2026-08-08 22:00 ~ 2026-08-09 01:00"
    # 跨年：两端都带年份
    assert SM._fmt_time_range("2026-12-31 23:00:00", "2027-01-01 00:30:00") == "2026-12-31 23:00 ~ 2027-01-01 00:30"
    assert SM._fmt_time_range("", "") == ""
    assert SM._fmt_time_range("2026-08-08 10:00:00", "") == ""
