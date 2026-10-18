"""记忆节点任务型 schema（v2）：字段落树 / 节点文本渲染 / 旧数据兼容与降级。"""

import time

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree
from planner.middleware import (
    NODE_MARKER,
    MemoryNodeOutput,
    SummarizationMiddleware,
)
from planner.session import PlannerSession


def _drive(session, content, trigger="player"):
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response(trigger)


def _wait_compress(session, timeout=15.0):
    """触发异步压缩并等待后台线程完成（_compressing 复位 = 应用已结束）。

    不能以"节点数增加"判断——父压缩轮会删除子节点消息（节点数反而减少）。
    """
    deadline = time.time() + timeout
    while getattr(session, "_compressing", False) and time.time() < deadline:
        time.sleep(0.05)
    session._maybe_compress_async()
    if not getattr(session, "_compressing", False):
        return   # 未达阈值，无压缩
    deadline = time.time() + timeout
    while getattr(session, "_compressing", False) and time.time() < deadline:
        time.sleep(0.1)
    if session._compressing:
        raise AssertionError("异步压缩超时未完成")


def test_compress_merges_messages_arrived_during_compression(data_root, monkeypatch):
    """压缩期间新到的消息保留：压缩线程基于快照，应用按 id 删除旧部分。

    回归：压缩不能覆盖 buffer——期间用户消息/模型生成的新消息必须保留。
    """
    import planner.middleware as MW
    from langchain_core.messages import HumanMessage as HM
    from planner.session import PlannerSession as PS

    orig_call = MW.SummarizationMiddleware._call_compress

    def slow_call(self, msgs, instruction):
        time.sleep(1.2)   # 拉长压缩窗口，确保压缩期间有新消息进来
        return orig_call(self, msgs, instruction)

    monkeypatch.setattr(MW.SummarizationMiddleware, "_call_compress", slow_call)
    s = PS(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000})   # 叶子压缩只看 token 预算
        for i in range(60):
            s.recent_buffer.append(HM(content=f"占位消息 {i} " + "x" * 200))
        s._msg_counter += 60
        s._maybe_compress_async()          # 触发后台压缩（压缩窗口 1.2s）
        assert s._compressing, "压缩线程应已启动"
        # 压缩期间新消息到来（模拟用户说话）
        time.sleep(0.4)
        new_msg = __import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(
            content="压缩期间的新消息", id="fresh-msg-1")
        s.recent_buffer.append(new_msg)
        s._msg_counter += 1
        # 等压缩完成
        deadline = time.time() + 15
        while s._compressing and time.time() < deadline:
            time.sleep(0.1)
        assert not s._compressing, "压缩应已完成"
        # 新消息保留（合并），节点出现
        assert any(getattr(m, "id", None) == "fresh-msg-1" for m in s.recent_buffer), \
            "压缩期间的新消息必须保留"
        assert any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer), "压缩应产生节点"
    finally:
        s.close()


def _build_session_with_batch(data_root, n=65):
    """建会话并灌 n 条带填充的消息：叶子压缩只看 token 预算（调到下限快速触发）。"""
    s = PlannerSession(data_root, mock=True)
    s.update_settings({"compress_token_budget": 10000})
    for i in range(n):
        s._append_to_buffer({"role": "user",
                             "content": f"用户说：第{i}条测试消息" + "填" * 120})
    return s


def test_leaf_node_has_task_fields(data_root):
    """叶子节点落树：meta.fields 带任务型字段；不再写 profile 画像列。"""
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        _wait_compress(s)
        tree = s.get_memory_tree()
        assert tree.get_level_count(0) == 1, "应落 1 个叶子"
        node = tree.get_nodes_at_level(0)[0]
        fields = node["meta"]["fields"]
        assert fields["task"]["goal"] == "按时完成学习计划"
        assert fields["task"]["status"] == "按计划推进"
        assert "喜欢晚上学习" in fields["user"]
        assert fields["decisions"][0]["why"] == "基础不牢会影响后续"
        assert node["profile"] is None, "新 schema 不再写画像列"
    finally:
        s.close()


def test_node_message_renders_full_fields_and_marker(data_root):
    """buffer 中的节点消息：全字段 + 层级 + 保护标记（常驻上下文）。"""
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        _wait_compress(s)
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        node_msgs = [m for m in s.recent_buffer
                     if (getattr(m, "metadata", None) or {}).get("node_id") == node["id"]]
        assert node_msgs, "buffer 中应有节点消息"
        text = node_msgs[0].content
        assert "[摘要]" in text
        assert "[任务]" in text and "目标：按时完成学习计划" in text
        assert "[决策]" in text and "因为" in text
        assert "[用户]" in text and "喜欢晚上学习" in text
        assert "· L0 ·" in text and NODE_MARKER in text
    finally:
        s.close()


def test_parent_node_rolls_up_fields(data_root):
    """父节点：字段上卷 + 消息带标记 + 时间范围。"""
    s = _build_session_with_batch(data_root)
    try:
        # 每轮灌 65 条带填充消息（越过 token 预算）产生 1 个叶子，攒够 6 个触发向上压缩
        for rnd in range(8):
            base = rnd * 100
            for i in range(65):
                s._append_to_buffer({"role": "user",
                                     "content": f"第{base+i}条讨论" + "填" * 120})
            _drive(s, f"（心跳{rnd}）继续", "player")
            _wait_compress(s)
            if s.get_memory_tree().get_level_count(1) >= 1:
                break
        tree = s.get_memory_tree()
        assert tree.get_level_count(1) >= 1, "应产生父节点"
        parent = tree.get_nodes_at_level(1)[0]
        assert parent["meta"]["fields"]["task"]["goal"], "父节点任务字段应上卷"
        p_msgs = [m for m in s.recent_buffer
                  if (getattr(m, "metadata", None) or {}).get("node_id") == parent["id"]]
        assert p_msgs and "[摘要]" in p_msgs[0].content
        assert "· L1 ·" in p_msgs[0].content and NODE_MARKER in p_msgs[0].content
    finally:
        s.close()


def test_explore_returns_fields_and_details(data_root):
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        _wait_compress(s)
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        info = tree.get_node_children_info(node["id"])
        assert info["meta"]["fields"]["user"]
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
        _wait_compress(s)
        tree = s.get_memory_tree()
        assert tree.get_level_count(0) == 1
        node = tree.get_nodes_at_level(0)[0]
        assert node["summary"], "降级后仍有摘要"
        fields = node["meta"]["fields"]
        assert fields["task"]["goal"] == "" and fields["user"] == []
    finally:
        s.close()


def test_profile_migration_old_db(data_root):
    """旧库（无 profile/meta 列）迁移后正常使用；旧节点兼容渲染。"""
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
        # 迁移后：旧节点可查，新叶子带 meta.fields
        info = tree.get_node_children_info("node0_001")
        assert info["details"] == []
        assert info["profile"] is None
        nid = tree.add_leaf("新摘要", (0, 1), None, profile={"preferences": ["x"]})
        assert tree.get_node_children_info(nid)["profile"] == {"preferences": ["x"]}
        # 旧节点（无 meta.fields）用旧渲染（画像标签）
        old = tree.get_nodes_at_level(0)
        old_node = next(x for x in old if x["id"] == "node0_001")
        text = SummarizationMiddleware._node_full_text(old_node)
        assert "[摘要] 旧摘要" in text
    finally:
        tree.close()


def test_render_node_text_omits_empty_fields_and_toggles_marker():
    out = MemoryNodeOutput(summary="摘要内容", user=["喜欢简洁"])
    text = SummarizationMiddleware._render_node_text("node0_001", 0, 10, out)
    assert "[摘要] 摘要内容" in text
    assert "[用户] 喜欢简洁" in text
    assert "[任务]" not in text and "[决策]" not in text and "[经验]" not in text
    assert "· L0 ·" in text and NODE_MARKER in text
    # 变体：去掉保护标记（节点压缩时的目标节点输入版本）
    text2 = SummarizationMiddleware._render_node_text("node0_001", 0, 10, out, marker=False)
    assert NODE_MARKER not in text2 and "[摘要] 摘要内容" in text2


def test_node_full_text_renders_new_fields():
    n = {
        "id": "node1_001", "level": 1, "summary": "阶段摘要",
        "round_range": [0, 79], "time_start": "2026-08-08 10:00:00",
        "time_end": "2026-08-08 12:00:00",
        "meta": {"schema_version": 2, "fields": {
            "task": {"goal": "交付 v1", "status": "开发中", "todo": ["补测试"],
                     "next_steps": [], "blockers": ["等待评审"]},
            "decisions": [{"what": "用方案 B", "why": "更快"}],
            "lessons": {"errors": [{"symptom": "超时", "cause": "锁竞争", "fix": "缩短事务"}],
                        "patterns": ["先复现再修"]},
            "context": {"env": ["Python 3.13"], "commands": ["pytest -q"],
                        "artifacts": ["src/main.py"], "constraints": ["不改接口"]},
            "user": ["偏好中文注释"],
        }},
    }
    text = SummarizationMiddleware._node_full_text(n)
    assert "[node1_001 · L1 · 第0-79条]" in text
    assert "[摘要] 阶段摘要" in text and "[时间] 2026-08-08 10:00 ~ 12:00" in text
    assert "目标：交付 v1" in text and "补测试" in text
    assert "用方案 B（因为 更快）" in text
    assert "超时→缩短事务（原因：锁竞争）" in text
    assert "约束：不改接口" in text and "[用户] 偏好中文注释" in text
    assert NODE_MARKER not in text          # 树渲染版本不带保护标记


def test_node_meta_schema_version(data_root):
    """节点落树带 meta.schema_version=2 与 fields；查询返回解析。"""
    s = _build_session_with_batch(data_root)
    try:
        _drive(s, "（心跳）继续", "player")
        _wait_compress(s)
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        assert node["meta"]["schema_version"] == 2
        assert "fields" in node["meta"]
        info = tree.get_node_children_info(node["id"])
        assert info["meta"]["schema_version"] == 2
    finally:
        s.close()


def test_unknown_fields_discarded(data_root):
    """extra=ignore：模型输出未知字段被丢弃，不落库。"""
    import json as _json

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from planner.llm import MockChatModel
    s = _build_session_with_batch(data_root)
    try:
        weird = MockChatModel(session=s)

        def fake_gen(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=_json.dumps({
                "summary": "摘要", "user": ["a"],
                "task": {"goal": "g"}, "unknown_field": "应被丢弃",
            }, ensure_ascii=False)))])

        weird._generate = fake_gen.__get__(weird)
        s._summary_model = weird
        s.set_chat_model(MockChatModel(session=s))
        _drive(s, "（心跳）继续", "player")
        _wait_compress(s)
        tree = s.get_memory_tree()
        node = tree.get_nodes_at_level(0)[0]
        assert node["summary"] == "摘要"
        assert node["meta"]["fields"]["user"] == ["a"]
        assert node["meta"]["fields"]["task"]["goal"] == "g"
        assert "unknown_field" not in node["meta"]
    finally:
        s.close()


def test_apply_budget_trims_long_fields():
    """节点预算软裁剪：超长列表/条目被截断。"""
    out = MemoryNodeOutput(
        summary="长" * 5000,
        task={"goal": "g" * 500, "todo": [f"t{i}" for i in range(20)]},
        decisions=[{"what": f"d{i}", "why": "w"} for i in range(20)],
        user=[f"u{i}" for i in range(30)],
    )
    trimmed = SummarizationMiddleware._apply_budget(out, 1000)
    assert len(trimmed.summary) <= 2000 + 1
    assert len(trimmed.task.todo) == 8
    assert len(trimmed.decisions) == 8
    assert len(trimmed.user) == 12
    assert len(trimmed.task.goal) <= 300
