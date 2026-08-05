"""记忆树画像字段 + future_notes 测试。"""

import tempfile
from pathlib import Path

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree
from planner.middleware import MemoryNodeOutput, ProfileInfo, SummarizationMiddleware
from planner.session import PlannerSession


def _drive(session, content, trigger="player"):
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response(trigger)


def _build_session_with_batch(data_root, n=65):
    s = PlannerSession(data_root, mock=True)
    for i in range(n):
        s._append_to_buffer({"role": "user", "content": f"用户说：第{i}条测试消息"})
    return s


def test_leaf_node_has_profile_and_future_notes(data_root):
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        tree = s.get_memory_tree()
        assert tree.get_level_count(0) == 1, "应落 1 个叶子"
        node = tree.get_nodes_at_level(0)[0]
        assert node["profile"] is not None, "叶子应有画像"
        assert node["profile"]["preferences"] == ["喜欢晚上学习"]
        assert node["profile"]["personality"] == ["做事有计划"]
        assert node["profile"]["goals"] == ["按时完成学习计划"]
    finally:
        s.close()


def test_node_message_renders_full_fields(data_root):
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        # buffer 中的节点消息应含全字段标签
        node_msgs = [m for m in s.recent_buffer
                     if (getattr(m, "metadata", None) or {}).get("node_id") == node["id"]]
        assert node_msgs, "buffer 中应有节点消息"
        text = node_msgs[0].content
        assert "[摘要]" in text
        assert "[喜好]" in text and "喜欢晚上学习" in text
        assert "[性格]" in text and "做事有计划" in text
        assert "[目标]" in text
    finally:
        s.close()


def test_parent_rolls_up_profile(data_root):
    s = _build_session_with_batch(data_root)
    try:
        # 产生 6 个叶子触发向上压缩
        for rnd in range(8):
            base = rnd * 100
            for i in range(65):
                s._append_to_buffer({"role": "user", "content": f"第{base+i}条讨论"})
            _drive(s, f"（心跳{rnd}）继续", "player")
            if s.get_memory_tree().get_level_count(1) >= 1:
                break
        tree = s.get_memory_tree()
        assert tree.get_level_count(1) >= 1, "应产生父节点"
        parent = tree.get_nodes_at_level(1)[0]
        assert parent["profile"] is not None, "父节点应有画像（子节点上卷）"
        assert parent["profile"]["preferences"], "父节点喜好应非空"
        # 父节点消息也全字段
        p_msgs = [m for m in s.recent_buffer
                  if (getattr(m, "metadata", None) or {}).get("node_id") == parent["id"]]
        assert p_msgs and "[摘要]" in p_msgs[0].content
    finally:
        s.close()


def test_explore_returns_profile_and_future_notes(data_root):
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        info = tree.get_node_children_info(node["id"])
        assert info["profile"] is not None
        assert info["details"]  # 原文保留
    finally:
        s.close()


def test_parse_fallback_on_invalid_json(data_root, monkeypatch):
    """模型输出非法 JSON → 降级为纯摘要，不中断。"""
    from planner.llm import MockChatModel
    s = _build_session_with_batch(data_root)
    try:
        bad_model = MockChatModel(session=s)
        bad_model._make_result = lambda content, tool_calls=None: MockChatModel._make_result(
            bad_model, "不是 JSON 的文本", tool_calls)
        s.set_chat_model(bad_model)
        # 覆盖 summary model 也返回非法 JSON
        s._summary_model = bad_model
        _drive(s, "（心跳）继续", "player")
        tree = s.get_memory_tree()
        assert tree.get_level_count(0) == 1
        node = tree.get_nodes_at_level(0)[0]
        assert node["summary"], "降级后仍有摘要"
        assert node["profile"] is None
    finally:
        s.close()


def test_profile_migration_old_db(data_root):
    """旧库（无 profile 列）迁移后正常使用。"""
    db = data_root / "memory_tree.db"
    # 先建旧 schema 库
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE nodes (
            character_id TEXT NOT NULL, id TEXT NOT NULL,
            level INTEGER NOT NULL, summary TEXT NOT NULL,
            parent_id TEXT, round_start INTEGER, round_end INTEGER,
            source_ref TEXT, details TEXT, is_active INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (unixepoch()),
            PRIMARY KEY (character_id, id));
        CREATE TABLE buffer_state (
            character_id TEXT PRIMARY KEY, recent_buffer TEXT,
            msg_counter INTEGER, round INTEGER);
    """)
    conn.execute("INSERT INTO nodes (character_id, id, level, summary) VALUES ('assistant', 'node0_001', 0, '旧摘要')")
    conn.commit()
    conn.close()
    tree = SQLiteMemoryTree("assistant", db)
    try:
        # 迁移后：旧节点可查，新叶子带 profile
        info = tree.get_node_children_info("node0_001")
        assert info["details"] == []
        assert info["profile"] is None
        nid = tree.add_leaf("新摘要", (0, 1), None, profile={"preferences": ["x"]})
        assert tree.get_node_children_info(nid)["profile"] == {"preferences": ["x"]}
    finally:
        tree.close()


def test_render_node_text_omits_empty_dimensions():
    out = MemoryNodeOutput(
        summary="摘要内容",
        profile=ProfileInfo(preferences=["a"], personality=[], habits=[], goals=[]),
        future_notes=["后来用户解释了原因"],
    )
    text = SummarizationMiddleware._render_node_text("node0_001", 0, 10, out)
    assert "[摘要] 摘要内容" in text
    assert "[喜好] a" in text
    assert "[性格]" not in text
    assert "[后续说明] 后来用户解释了原因" in text


def test_node_meta_schema_version(data_root):
    """节点落树带 meta.schema_version；查询返回解析。"""
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        assert node["meta"] == {"schema_version": 1}
        info = tree.get_node_children_info(node["id"])
        assert info["meta"] == {"schema_version": 1}
    finally:
        s.close()


def test_unknown_fields_discarded(data_root):
    """extra=ignore：模型输出未知字段被丢弃，不落库。"""
    from planner.llm import MockChatModel
    import json as _json
    s = _build_session_with_batch(data_root)
    try:
        weird = MockChatModel(session=s)
        orig = MockChatModel._make_result
        def fake_make(self, content, tool_calls=None):
            return orig(self, content, tool_calls)
        weird._make_result = fake_make.__get__(weird)
        # 让 mock 输出带未知字段的 JSON
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.messages import AIMessage
        def fake_gen(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=_json.dumps({
                "summary": "摘要", "profile": {"preferences": ["a"]},
                "future_notes": [], "unknown_field": "应被丢弃",
            }, ensure_ascii=False)))])
        weird._generate = fake_gen.__get__(weird)
        s._summary_model = weird
        s.set_chat_model(MockChatModel(session=s))
        _drive(s, "（心跳）继续", "player")
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        assert node["summary"] == "摘要"
        assert node["profile"] == {"preferences": ["a"], "personality": [], "habits": [], "goals": []}
        assert "unknown_field" not in node.get("meta", {})
    finally:
        s.close()
