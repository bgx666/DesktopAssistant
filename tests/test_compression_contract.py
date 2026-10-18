"""压缩输入边界契约（计划 六/七节）：

- 叶子压缩输入 = [带保护标记的已有节点] + [本次待压原始消息]（保留的尾部不进输入）
- 节点压缩输入 = 目标节点去保护标记、其他节点保留；指令点名目标
- 指令含保护标记说明与目标范围
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from planner.middleware import (
    NODE_MARKER,
    MemoryNodeOutput,
    SummarizationMiddleware,
)
from planner.session import PlannerSession


def _capture(comp, monkeypatch, reply_summary="摘要"):
    captured = {}

    def fake(ctx, instruction):
        captured["ctx"] = list(ctx)
        captured["instruction"] = instruction.content
        return MemoryNodeOutput(summary=reply_summary)

    monkeypatch.setattr(comp, "_call_compress", fake)
    return captured


def _contents(captured) -> list[str]:
    return [str(getattr(m, "content", "")) for m in captured["ctx"]]


def test_leaf_input_excludes_kept_tail(data_root, monkeypatch):
    """叶子压缩输入只含待压的中间段；保留的尾部原始消息不进输入。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 10})
        msgs = [HumanMessage(content=f"m{i}" + "x" * 298,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(30)]
        comp = SummarizationMiddleware(s)
        captured = _capture(comp, monkeypatch)
        comp.compress_snapshot(msgs)
        texts = _contents(captured)
        assert any(t.startswith("m0") for t in texts)
        assert any(t.startswith("m19") for t in texts)
        assert all(not t.startswith("m20") for t in texts), "保留的尾部不应进压缩输入"
        assert all(not t.startswith("m29") for t in texts)
        # 指令：含保护标记说明 + 目标范围点名
        assert NODE_MARKER in captured["instruction"]
        assert "0-19" in captured["instruction"]
        # 空字段规则：没有内容就省略/留空、不写占位词、不硬凑
        ins = captured["instruction"]
        assert "整个字段可以直接省略" in ins
        assert "暂无" in ins and "占位词" in ins
    finally:
        s.close()


def test_leaf_input_includes_marked_nodes(data_root, monkeypatch):
    """上下文里的已有节点（带保护标记）进叶子压缩输入，供模型识别"不要动"。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 10})
        node_msg = HumanMessage(
            content=(f"[node1_001 · L1 · 第0-39条] {NODE_MARKER}\n[摘要] 旧摘要"),
            metadata={"node_id": "node1_001", "node_start": 0})
        msgs = [node_msg] + [HumanMessage(content=f"m{i}" + "x" * 298,
                                          metadata={"ts": "2026-08-08 10:00:00"})
                             for i in range(30)]
        comp = SummarizationMiddleware(s)
        captured = _capture(comp, monkeypatch)
        comp.compress_snapshot(msgs)
        texts = _contents(captured)
        assert any("旧摘要" in t and NODE_MARKER in t for t in texts), \
            "已有节点应带保护标记进入输入"
    finally:
        s.close()


def test_compact_strips_marker_on_targets_and_names_them(data_root, monkeypatch):
    """节点压缩：目标节点去掉保护标记（+ 指令点名），其他节点保留标记。"""
    s = PlannerSession(data_root, mock=True)
    try:
        tree = s.get_memory_tree()
        msgs = []
        for i in range(8):
            nid = tree.add_leaf(f"摘要{i}", (i * 2, i * 2 + 1), None)
            msgs.append(HumanMessage(
                content=f"[{nid} · L0 · 第{i * 2}-{i * 2 + 1}条] {NODE_MARKER}\n[摘要] 摘要{i}",
                metadata={"node_id": nid, "node_start": i * 2}))
        comp = SummarizationMiddleware(s)
        captured = _capture(comp, monkeypatch, reply_summary="父摘要")
        comp.compress_snapshot(msgs)
        by_node = {}
        for m in captured["ctx"]:
            nid = (getattr(m, "metadata", None) or {}).get("node_id")
            if nid:
                by_node[nid] = str(getattr(m, "content", ""))
        # factor=4：目标是前 4 个（node0_001..node0_004）
        assert set(by_node) == {f"node0_{i:03d}" for i in range(1, 9)}
        for i in range(1, 5):
            assert NODE_MARKER not in by_node[f"node0_{i:03d}"], "目标应去掉保护标记"
        for i in range(5, 9):
            assert NODE_MARKER in by_node[f"node0_{i:03d}"], "非目标应保留保护标记"
        # 指令点名目标
        for i in range(1, 5):
            assert f"node0_{i:03d}" in captured["instruction"]
        assert "node0_005" not in captured["instruction"]
        # 空字段规则（父层同样强化：可省略、不写占位词）
        assert "整个省略不写" in captured["instruction"]
        assert "占位词" in captured["instruction"]
        # 输入不含原始消息（节点压缩只喂节点）
        assert all((getattr(m, "metadata", None) or {}).get("node_id")
                   for m in captured["ctx"])
    finally:
        s.close()


def test_should_compress_raw_conditions():
    """触发条件：只看原始消息估算 token ≥ 预算（条数不再参与判定）。"""
    from planner.middleware import should_compress_raw
    small = [HumanMessage(content="x" * 100) for _ in range(5)]
    # 500 字符 × 1.2 = 600 < 100000 → 不触发（哪怕条数很多）
    assert not should_compress_raw(small, keep=2, token_budget=100000)
    assert not should_compress_raw(small, keep=2, token_budget=1000)
    # 超预算 → 触发（与条数无关）
    assert should_compress_raw(small, keep=2, token_budget=400)
    # 少于 2 条 / 预算非法 → 不触发
    assert not should_compress_raw(small[:1], keep=0, token_budget=1)
    assert not should_compress_raw(small, keep=2, token_budget=0)


def test_token_budget_triggers_compression(data_root, monkeypatch):
    """大工具输出（token 超预算）触发叶子压缩；保留 keep 条。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_keep": 5, "compress_token_budget": 10000})
        big = "x" * 3000            # 8 × 3000 字 × 1.2 ≈ 28800 token ≥ 10000
        msgs = [HumanMessage(content=big, metadata={"ts": "2026-08-08 10:00:00"})
                for _ in range(8)]
        comp = SummarizationMiddleware(s)
        _capture(comp, monkeypatch)
        removes, adds = comp.compress_snapshot(msgs)
        assert removes and adds, "token 超预算应触发叶子压缩"
        assert len(removes) == 3    # 8 - keep(5)
    finally:
        s.close()


def test_token_budget_huge_tail_keeps_last_message(data_root, monkeypatch):
    """连 keep 条都超预算（超大工具输出）→ 只保留最后 1 条，避免永远圈不出批次。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_keep": 5, "compress_token_budget": 10000})
        big = "x" * 5000            # 5 条 × 5000 字 × 1.2 = 30000 token ≥ 预算
        msgs = [HumanMessage(content=big, metadata={"ts": "2026-08-08 10:00:00"})
                for _ in range(5)]  # 条数(5) == keep → 正常规则圈不出批次
        comp = SummarizationMiddleware(s)
        _capture(comp, monkeypatch)
        removes, adds = comp.compress_snapshot(msgs)
        assert removes and adds, "超预算时至少要压得动（保留最后 1 条）"
        assert len(removes) == 4
    finally:
        s.close()


def test_merge_cwd():
    """父节点工作目录 = 子节点 context.cwd 去重合并（≤3，多则省略）。"""
    from planner.middleware import SummarizationMiddleware as SM

    def node(cwd):
        return {"meta": {"fields": {"context": {"cwd": cwd}}}}

    assert SM._merge_cwd([node("A"), node("A")]) == "A"
    assert SM._merge_cwd([node("A"), node("B")]) == "A；B"
    assert SM._merge_cwd([node(""), node("A")]) == "A"
    assert SM._merge_cwd([node("")]) == ""
    assert SM._merge_cwd([]) == ""
    assert SM._merge_cwd([node(f"D{i}") for i in range(5)]) == "D0；D1；D2…"


def test_leaf_records_cwd(data_root, monkeypatch, tmp_path):
    """叶子节点 context.cwd = 压缩时的工作目录（代码填写，不靠模型）。"""
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"workspace_root": str(ws), "workspace_cwd": str(ws / "src")})
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 10})
        comp = SummarizationMiddleware(s)
        captured = _capture(comp, monkeypatch)
        msgs = [HumanMessage(content=f"m{i}" + "x" * 298,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(30)]
        comp.compress_snapshot(msgs)
        # 压缩指令里带了当前工作目录提示
        assert str((ws / "src").resolve()) in captured["instruction"]
        node = s.get_memory_tree().get_nodes_at_level(0)[0]
        assert node["meta"]["fields"]["context"]["cwd"] == str((ws / "src").resolve())
    finally:
        s.close()


def test_compact_merges_child_cwd(data_root, monkeypatch, tmp_path):
    """父节点 cwd 从子节点合并（构造两个不同 cwd 的叶子）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        tree = s.get_memory_tree()
        msgs = []
        for i in range(8):
            cwd = f"D:/proj{i % 2}"          # 两个不同目录交替
            nid = tree.add_leaf(
                f"摘要{i}", (i * 2, i * 2 + 1), None,
                meta={"schema_version": 2, "fields": {"context": {"cwd": cwd}}})
            msgs.append(HumanMessage(
                content=f"[{nid} · L0 · 第{i * 2}-{i * 2 + 1}条] {NODE_MARKER}\n[摘要] 摘要{i}",
                metadata={"node_id": nid, "node_start": i * 2}))
        comp = SummarizationMiddleware(s)
        _capture(comp, monkeypatch, reply_summary="父摘要")
        s._compressed_total = 0
        comp.compress_snapshot(msgs)
        parent = tree.get_nodes_at_level(1)[0]
        assert parent["meta"]["fields"]["context"]["cwd"] == "D:/proj0；D:/proj1"
    finally:
        s.close()


def test_mock_compression_output_matches_new_schema(data_root, monkeypatch):
    """完整链路（mock 摘要模型）：节点落树字段齐备、节点消息带标记与层级。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000})   # 只看 token 预算
        for i in range(65):
            s._append_to_buffer({"role": "user", "content": f"第{i}条" + "填" * 160})
        s._receive("（心跳）继续", trigger=True)
        s.pending_response = False
        s._generate_response("player")
        import time
        deadline = time.time() + 15
        while getattr(s, "_compressing", False) and time.time() < deadline:
            time.sleep(0.05)
        s._maybe_compress_async()
        deadline = time.time() + 15
        while getattr(s, "_compressing", False) and time.time() < deadline:
            time.sleep(0.1)
        tree = s.get_memory_tree()
        assert tree.get_level_count(0) >= 1
        node = tree.get_nodes_at_level(0)[0]
        fields = node["meta"]["fields"]
        assert set(fields) == {"task", "decisions", "lessons", "context", "user"}
        assert node["profile"] is None
        node_msgs = [m for m in s.recent_buffer
                     if (getattr(m, "metadata", None) or {}).get("node_id") == node["id"]]
        assert node_msgs and NODE_MARKER in node_msgs[0].content
    finally:
        s.close()


# ── 2026-10-18：压缩空转修复（摘要模型关思考 + 超大批次分片 + 降级标记）──

def test_compress_one_shot_single_node(data_root, monkeypatch):
    """整批优先：批次正常大小时**一次调用 = 一个节点**（用户预期，2026-10-18）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5})
        msgs = [HumanMessage(content=f"m{i:03d}" + "x" * 999,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(60)]
        comp = SummarizationMiddleware(s)
        calls = []

        def fake(ctx, instruction):
            calls.append([m for m in ctx
                          if str(getattr(m, "content", "")).startswith("m")])
            return MemoryNodeOutput(summary="摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        removes, adds = comp.compress_snapshot(msgs)
        assert len(calls) == 1, "整批一次调用"
        assert len(adds) == 1, "一次压缩 = 一个节点"
        assert len(removes) == 55   # 60 - keep 5
        assert len(calls[0]) == 55  # 节点范围覆盖整个批次
    finally:
        s.close()


def test_compress_falls_back_to_chunks_on_failure(data_root, monkeypatch):
    """整批调用失败（如超窗）→ 自动回退分片重试（每片各落一个节点）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5})
        msgs = [HumanMessage(content=f"m{i:03d}" + "x" * 999,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(60)]
        comp = SummarizationMiddleware(s)
        calls = []

        def fake(ctx, instruction):
            batch = [m for m in ctx if str(getattr(m, "content", "")).startswith("m")]
            calls.append(batch)
            if len(batch) > 30:          # 模拟整批超窗失败
                return None
            return MemoryNodeOutput(summary="摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        removes, adds = comp.compress_snapshot(msgs)
        assert len(calls) >= 3, "第一次整批失败 + 至少两片重试"
        assert len(adds) == len(calls) - 1, "每片各产生一个节点（整批那次失败不落）"
        assert len(removes) == 55
        for batch in calls[1:]:
            est = sum(int(len(str(m.content)) * 1.2) for m in batch)
            assert est <= 30000 + 1300, f"分片过大: {est}"
    finally:
        s.close()


def test_split_batch_respects_tool_pairs():
    """分片不在 ai(tool_calls)/tool 之间切（配对断裂 → 请求 400）。"""
    from langchain_core.messages import AIMessage, ToolMessage

    batch = []
    for i in range(6):
        batch.append(AIMessage(content="a" * 999,
                               tool_calls=[{"id": f"c{i}", "name": "t", "args": {}}]))
        batch.append(ToolMessage(content="t" * 999, tool_call_id=f"c{i}", name="t"))
    chunks = SummarizationMiddleware._split_batch_by_tokens(batch, max_tokens=1500)
    assert len(chunks) >= 2
    for ch in chunks:
        assert getattr(ch[0], "type", None) != "tool", "分片不能以 tool 开头"
        assert not getattr(ch[-1], "tool_calls", None), "分片不能以带 tool_calls 的 ai 结尾"


def test_summary_model_thinking_disabled(data_root, monkeypatch):
    """压缩模型显式关思考（开思考会吃光 max_tokens → 摘要为空 → 压缩空转）。"""
    import planner.session as session_mod

    captured = {}

    def fake_build(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(session_mod, "build_chat_model", fake_build)
    s = PlannerSession(data_root, mock=False)
    try:
        s._get_summary_model()
        assert captured.get("thinking_mode") == "disabled"
        assert captured.get("max_tokens") == 3000   # node_token_budget 1000 × 3
    finally:
        s.close()


def test_invoke_compress_skips_structured_after_failure(data_root, monkeypatch):
    """structured 失败打标后，后续调用不再尝试（省每次注定失败的 ~15s 等待）。"""
    import types as _types

    s = PlannerSession(data_root, mock=True)
    try:
        comp = SummarizationMiddleware(s)
        calls = {"structured": 0, "plain": 0}

        class _FakeModel:
            def with_structured_output(self, *a, **k):
                calls["structured"] += 1
                raise RuntimeError("endpoint does not support json_mode")

            def invoke(self, req):
                calls["plain"] += 1
                return _types.SimpleNamespace(content='{"summary": "压缩结果"}')

        monkeypatch.setattr(s, "_get_summary_model", lambda: _FakeModel())
        instr = HumanMessage(content="压缩指令")
        out1 = comp._invoke_compress([], instr)
        out2 = comp._invoke_compress([], instr)
        assert out1 is not None and out1.summary == "压缩结果"
        assert out2 is not None and out2.summary == "压缩结果"
        assert calls["structured"] == 1, "第二次不应再试 structured"
        assert getattr(s, "_compress_no_structured", False) is True
    finally:
        s.close()


def test_clean_wrapped_summary():
    """JSON 外壳摘要剥壳：完整 / 截断（含断在字符串中间）/ 非 JSON。"""
    from planner.middleware import clean_wrapped_summary

    assert clean_wrapped_summary('{"summary": "纯文本摘要"}') == "纯文本摘要"
    assert clean_wrapped_summary('{"summary": "a\\nb", "task": {}}') == "a\nb"
    # 截断：summary 字符串没闭合（EOF while parsing a string 场景）
    assert clean_wrapped_summary('{"summary": "被截断的摘') == "被截断的摘"
    # 截断：summary 完整但后面还有半截字段
    assert clean_wrapped_summary('{"summary": "完整摘要", "task": {"goal": "x') == "完整摘要"
    assert clean_wrapped_summary("正常文本") == "正常文本"
    assert clean_wrapped_summary("") == ""


def test_invoke_compress_salvages_truncated_json(data_root, monkeypatch):
    """两次调用都返回截断 JSON → 抠出 summary 存为纯摘要（而不是把 JSON 整段存进去）。"""
    import types as _types

    s = PlannerSession(data_root, mock=True)
    try:
        comp = SummarizationMiddleware(s)

        class _FakeModel:
            def with_structured_output(self, *a, **k):
                raise RuntimeError("no json_mode")

            def invoke(self, req):
                return _types.SimpleNamespace(
                    content='{"summary": "被截断的摘要内容", "task": {"goa')

        monkeypatch.setattr(s, "_get_summary_model", lambda: _FakeModel())
        out = comp._invoke_compress([], HumanMessage(content="压缩指令"))
        assert out is not None
        assert out.summary == "被截断的摘要内容"
        assert not out.summary.startswith("{"), "不应把 JSON 外壳存成摘要"
    finally:
        s.close()


def test_state_dict_has_compressing_flag(data_root):
    """压缩状态进 state_dict（前端标题栏呼吸灯的数据来源）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        assert s.state_dict().get("compressing") is False
        s._compressing = True
        assert s.state_dict().get("compressing") is True
    finally:
        s.close()


def test_heal_wrapped_node_summaries(data_root):
    """启动自愈：缓冲里 JSON 外壳的节点消息被原地剥壳（非节点消息不动）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        from langchain_core.messages import HumanMessage as _HM

        node_msg = _HM(
            content='[node0_900 · L0 · 第1-3条] [已压缩 · 不需要压缩]\n'
                    '[摘要] {"summary": "真实摘要内容", "task": {}}\n[时间] 10:00 - 11:00',
            metadata={"node_id": "node0_900", "node_start": 1})
        plain = _HM(content='[摘要] {"summary": "不是节点"}')   # 无 node_id：不动
        with s.buffer_lock:
            s.recent_buffer.extend([node_msg, plain])
        s._heal_wrapped_node_summaries()
        assert "真实摘要内容" in str(node_msg.content)
        assert '{"summary"' not in str(node_msg.content)
        assert str(plain.content) == '[摘要] {"summary": "不是节点"}'
    finally:
        s.close()


def test_async_compression_applies_single_node(data_root, monkeypatch):
    """端到端（假摘要模型）：整批一次 → **一个节点**替换 buffer，raw 明显下降。

    回归"每次触发、每次空转、什么都不替换"：压缩必须真的落进 buffer。
    """
    import types as _types

    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5})
        with s.buffer_lock:
            for i in range(60):
                s.recent_buffer.append(HumanMessage(
                    content=f"m{i:03d}" + "x" * 999,
                    id=f"t{i:03d}",   # 生产环境消息都有 id（RemoveMessage 靠它对上）
                    metadata={"ts": "2026-08-08 10:00:00"}))

        class _FakeModel:
            def with_structured_output(self, *a, **k):
                raise RuntimeError("no json_mode")

            def invoke(self, req):
                return _types.SimpleNamespace(content='{"summary": "片段摘要"}')

        monkeypatch.setattr(s, "_get_summary_model", lambda: _FakeModel())
        snapshot = list(s.recent_buffer)
        s._run_async_compression(snapshot)   # 空闲态下直接同步跑
        raw = [m for m in s.recent_buffer
               if "node_id" not in (getattr(m, "metadata", None) or {})]
        nodes = [m for m in s.recent_buffer
                 if "node_id" in (getattr(m, "metadata", None) or {})]
        assert len(nodes) == 1, "整批优先：一次压缩应落一个节点"
        assert len(raw) == 5, f"keep=5 应保留 5 条原始消息，实际 {len(raw)}"
    finally:
        s.close()
