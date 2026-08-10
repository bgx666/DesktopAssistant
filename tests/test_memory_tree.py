"""SQLiteMemoryTree 存储层测试。"""

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree


def test_add_leaf_and_query(data_root):
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    nid = tree.add_leaf("第一条对话摘要", (0, 4), "src", details=[{"role": "user", "content": "hi"}])
    assert nid.startswith("node0_")
    info = tree.get_node_children_info(nid)
    assert info is not None
    assert info["details"][0]["content"] == "hi"
    assert tree.get_level_count(0) == 1
    tree.close()


def test_compact_and_active_marks(data_root):
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    ids = [tree.add_leaf(f"摘要{i}", (i * 2, i * 2 + 1), None) for i in range(3)]
    parent = tree.compact(ids, "合并摘要")
    assert parent.startswith("node1_")
    assert tree.get_level_count(0) == 0       # 子节点全部 inactive
    assert tree.get_level_count(1) == 1
    info = tree.get_node_children_info(parent)
    assert len(info["children"]) == 3
    assert info["children"][0]["node_id"] == ids[0]
    tree.close()


def test_compact_raises_on_empty(data_root):
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    try:
        tree.compact([], "x")
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    tree.close()


def test_get_root_id_and_explore_root(data_root):
    """get_root_id 返回最高层节点；explore_memory_tree 留空参数 → 根节点概览。"""
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    assert tree.get_root_id() is None   # 空树
    leaf_ids = [tree.add_leaf(f"摘要{i}", (i * 2, i * 2 + 1), None) for i in range(4)]
    p1 = tree.compact(leaf_ids[:2], "合并A")
    p2 = tree.compact(leaf_ids[2:], "合并B")
    root = tree.compact([p1, p2], "根摘要")
    assert tree.get_root_id() == root
    info = tree.get_node_children_info(tree.get_root_id())
    assert len(info["children"]) == 2   # 根下两个分支（node1_001/002 的概要）
    assert {c["node_id"] for c in info["children"]} == {p1, p2}
    tree.close()


def test_effective_time_range(data_root):
    """get_effective_time_range：自身有值直接返回；为 NULL 时聚合子节点 min/max。"""
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    t1, t2, t3, t4 = ("2026-08-01 09:00:00", "2026-08-02 18:00:00",
                      "2026-08-03 08:30:00", "2026-08-04 22:15:00")
    a = tree.add_leaf("摘要A", (0, 1), None, time_range=(t1, t2))
    b = tree.add_leaf("摘要B", (2, 3), None, time_range=(t3, t4))
    parent = tree.compact([a, b], "合并")
    assert tree.get_effective_time_range(parent) == (t1, t4), "父节点 = 子节点 min/max"
    # 历史数据：父节点时间戳为 NULL → 从子节点聚合
    tree._execute_with_retry(
        "UPDATE nodes SET time_start = NULL, time_end = NULL WHERE id = ?",
        (parent,))
    assert tree.get_effective_time_range(parent) == (t1, t4), "NULL 时应聚合子节点"
    tree.close()


def test_node_ids_resume_after_restart(data_root):
    db = data_root / "memory_tree.db"
    t1 = SQLiteMemoryTree("assistant", db)
    t1.add_leaf("a", (0, 1), None)
    t1.close()
    t2 = SQLiteMemoryTree("assistant", db)
    nid = t2.add_leaf("b", (2, 3), None)
    assert nid == "node0_002"
    t2.close()


def test_character_isolation(data_root):
    db = data_root / "memory_tree.db"
    t1 = SQLiteMemoryTree("alice", db)
    t2 = SQLiteMemoryTree("bob", db)
    t1.add_leaf("a", (0, 1), None)
    assert t2.get_level_count(0) == 0
    assert t1.get_level_count(0) == 1
    t1.close()
    t2.close()


def test_buffer_state_roundtrip(data_root):
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    tree.save_buffer_state([{"role": "user", "content": "hi"}], 7, 3)
    state = tree.load_buffer_state()
    assert state["recent_buffer"][0]["content"] == "hi"
    assert state["_msg_counter"] == 7
    assert state["round"] == 3
    tree.close()


def test_clear_character_data(data_root):
    tree = SQLiteMemoryTree("assistant", data_root / "memory_tree.db")
    tree.add_leaf("a", (0, 1), None)
    tree.save_buffer_state([], 0, 0)
    tree.clear_character_data()
    assert tree.get_level_count(0) == 0
    assert tree.load_buffer_state() is None
    tree.close()
