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
    """节点压缩：目标节点去掉保护标记（+ 指令点名）；**目标之后的节点（未来）
    不进输入**（与叶子剔除保留尾部同一原则）。"""
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
        # factor=4：目标是前 4 个（node0_001..node0_004）；之后的（005..008）不进输入
        assert set(by_node) == {f"node0_{i:03d}" for i in range(1, 5)}
        for i in range(1, 5):
            assert NODE_MARKER not in by_node[f"node0_{i:03d}"], "目标应去掉保护标记"
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


def test_instruction_carries_length_budget(data_root):
    """node_token_budget 必须写进压缩指令（叶子和父层），而不只是事后裁剪。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"node_token_budget": 1000})
        comp = SummarizationMiddleware(s)
        leaf = comp._leaf_instruction(1, 2)
        assert "长度预算" in leaf and "1000 字以内" in leaf
        assert "400 字" in leaf, "summary 约占总预算 40%"
        parent = comp._parent_instruction(["a", "b"])
        assert "长度预算" in parent and "1000 字以内" in parent
        comp.language = "en"
        assert "Length budget" in comp._leaf_instruction(1, 2)
        assert "Length budget" in comp._parent_instruction(["a", "b"])
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

def test_compress_small_batch_one_shot(data_root, monkeypatch):
    """小批次（≤ 分片大小）不分片：一次调用 = 一个节点。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5,
                           "compress_chunk_tokens": 100000})   # 分片阈值很大 → 不分片
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
        assert len(calls) == 1, "小批次一次调用"
        assert len(adds) == 1, "一次压缩 = 一个节点"
        assert len(removes) == 55   # 60 - keep 5
        assert len(calls[0]) == 55  # 节点范围覆盖整个批次
    finally:
        s.close()


def test_compress_chunked_progressive_prefix(data_root, monkeypatch):
    """分片（渐进前缀）：第 k 次调用带第 1..k 片原文（缓存命中形态），指令带
    头/尾锚点；n 个分片叶子**各自进上下文**（level 0 活跃，等层级合并消化）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5,
                           "compress_chunk_tokens": 10000,
                           "compact_threshold": 50})   # 阈值拉高：不让层级合并消化
        msgs = [HumanMessage(content=f"m{i:03d}" + "x" * 999,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(60)]
        comp = SummarizationMiddleware(s)
        calls = []   # (发送的原始消息条数, 指令文本)

        def fake(ctx, instruction):
            raw = [m for m in ctx if str(getattr(m, "content", "")).startswith("m")]
            calls.append((len(raw), str(instruction.content)))
            return MemoryNodeOutput(summary="摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        removes, adds = comp.compress_snapshot(msgs)
        n = len(calls)
        assert n >= 3, f"55 条×1000 字 ≈ 66k token，1 万/片应切 ≥3 片，实际 {n}"
        assert len(removes) == 55
        assert len(adds) == n, f"{n} 个分片叶子各自进上下文"
        # 渐进前缀：第 k 次调用发送的原文总条数逐次增长（= 第 1..k 片）
        sent = [c[0] for c in calls]
        assert sent == sorted(sent) and sent[0] < sent[-1], f"渐进前缀不增长: {sent}"
        for _, ins in calls:
            assert "从这条消息开始" in ins, "分片指令应带头部锚点定位"
            assert "直到输入的最后一条消息" in ins, "分片指令应带尾部锚点定位"
        # 树：n 个分片叶子留在 level 0（活跃）
        tree = s.get_memory_tree()
        assert tree.get_level_count(0) == n, "分片叶子留在 level 0（活跃）"
        assert tree.get_level_count(1) == 0, "不产生父节点"
    finally:
        s.close()


def test_compress_chunk_failure_stops_midway(data_root, monkeypatch):
    """某片失败 → 停手：已成功片照常进上下文（无父节点），后续片不再压。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5,
                           "compress_chunk_tokens": 10000})
        msgs = [HumanMessage(content=f"m{i:03d}" + "x" * 999,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(60)]
        comp = SummarizationMiddleware(s)
        state = {"n": 0}

        def fake(ctx, instruction):
            state["n"] += 1
            if state["n"] == 3:
                return None   # 第 3 片失败
            return MemoryNodeOutput(summary="摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        removes, adds = comp.compress_snapshot(msgs)
        assert state["n"] == 3, "第 3 片失败后停手（不再有调用）"
        assert len(adds) == 2, "已成功的两片各自一个叶子（跳过父合并）"
        assert len(removes) < 55, "只移除前两片覆盖的消息"
    finally:
        s.close()


def test_split_prefers_human_boundary():
    """切点策略：浮动窗口内优先用户消息边界（≥0.75×目标）；无 human 边界时
    到 1.4× 后在安全边界收片。"""
    from langchain_core.messages import AIMessage

    from planner.middleware import SummarizationMiddleware

    def msg(i, kind="human"):
        cls = HumanMessage if kind == "human" else AIMessage
        return cls(content=f"m{i:03d}" + "x" * 999,
                   metadata={"ts": "2026-08-08 10:00:00"})

    # 全 human：0.75×30000=22500 token ≈ 19 条（×1200）处收片
    batch = [msg(i) for i in range(40)]
    chunks = SummarizationMiddleware._split_batch_by_tokens(batch, max_tokens=30000)
    assert [len(c) for c in chunks] == [19, 19, 2], [len(c) for c in chunks]
    # 全 ai（无 human 边界）：到 1.4×42000 token ≈ 35~36 条才收
    batch2 = [msg(i, "ai") for i in range(50)]
    chunks2 = SummarizationMiddleware._split_batch_by_tokens(batch2, max_tokens=30000)
    assert 35 <= len(chunks2[0]) <= 36, f"无 human 边界应浮动到 1.4×，实际 {len(chunks2[0])}"
    assert len(chunks2) == 2


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


def test_async_compression_applies_leaves(data_root, monkeypatch):
    """端到端（假摘要模型）：分片压缩 → **分片叶子替换 buffer**，raw 明显下降。

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
        assert len(nodes) >= 2, "分片压缩：应落多个分片叶子节点"
        assert len(raw) == 5, f"keep=5 应保留 5 条原始消息，实际 {len(raw)}"
    finally:
        s.close()


def test_should_compress_raw_counts_images_by_384_not_base64():
    """图片块按每张 384 计，不按 base64 字符数——一张 1MB 图不得把估算撑过预算。

    回归用户实测：预算 20 万，原始消息（含视觉注入图片）才 1 万多就触发压缩。
    """
    from planner.middleware import should_compress_raw

    big = "data:image/png;base64," + "A" * 1_000_000
    img_msg = HumanMessage(content=[
        {"type": "text", "text": "看这张照片"},
        {"type": "image_url", "image_url": {"url": big}},
    ])
    msgs = [img_msg, HumanMessage(content="嗯，不错")]
    assert not should_compress_raw(msgs, keep=20, token_budget=200000), \
        "base64 不得按字符数计入估算"
    # 纯文本仍然按 chars×1.2（偏保守）
    text_msgs = [HumanMessage(content="x" * 200000), HumanMessage(content="y")]
    assert should_compress_raw(text_msgs, keep=20, token_budget=200000)
    # 图片按每张 384 计：预算小于图片总额时应当触发
    many = [HumanMessage(content=[{"type": "image_url",
                                   "image_url": {"url": big}}])
            for _ in range(3)]
    assert should_compress_raw(many, keep=20, token_budget=1000)


def test_compress_input_strips_image_base64(data_root, monkeypatch):
    """压缩调用输入剥离 image_url 块（base64 对摘要无用且极贵）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 10})
        big = "data:image/png;base64," + "A" * 500000
        msgs = [HumanMessage(content=[
                    {"type": "text", "text": "看这张照片"},
                    {"type": "image_url", "image_url": {"url": big}}],
                    metadata={"ts": "2026-08-08 10:00:00"})]
        msgs += [HumanMessage(content=f"m{i}" + "x" * 798,
                              metadata={"ts": "2026-08-08 10:00:00"})
                 for i in range(29)]
        comp = SummarizationMiddleware(s)
        captured = _capture(comp, monkeypatch)
        comp.compress_snapshot(msgs)
        texts = _contents(captured)
        assert texts, "文本量超预算应触发叶子压缩"
        assert all("base64" not in t for t in texts), "压缩输入不得带 base64"
        assert any("看这张照片" in t or "（图片）" in t for t in texts)
    finally:
        s.close()


def test_compact_drains_level_below_threshold(data_root, monkeypatch):
    """某层超阈值 → **一轮内持续合并到低于阈值**（旧行为每轮每层只合一批：
    10 个节点(6/3) 只合 3 个、剩 7 个仍超阈值挂到下一轮——用户指出）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compact_threshold": 6, "compact_factor": 3})
        tree = s.get_memory_tree()
        for i in range(10):
            tree.add_leaf(f"摘要{i}", (i * 5, i * 5 + 4), None)
        assert tree.get_level_count(0) == 10
        comp = SummarizationMiddleware(s)

        def fake(ctx, instruction):
            return MemoryNodeOutput(summary="合并摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        comp.compress_snapshot([])   # 无批次压缩，只跑层级合并
        assert tree.get_level_count(0) == 4, "10 → 7 → 4（持续合并到低于阈值）"
        assert tree.get_level_count(1) == 2, "两次合并各出一个父节点"
    finally:
        s.close()


def test_compact_excludes_nodes_after_targets(data_root, monkeypatch):
    """节点合并输入：只带目标**之前**的节点（更早背景、带标记）；目标之后的
    节点（未来信息）不进输入——与叶子压缩剔除保留尾部同一原则（用户定）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compact_threshold": 6, "compact_factor": 3})
        tree = s.get_memory_tree()
        early = [tree.add_leaf(f"早{i}", (i * 10, i * 10 + 9), None) for i in range(2)]
        lv1 = tree.compact(early, "更早的父节点")   # L1：第 0-19 条（目标之前）
        ids = [tree.add_leaf(f"摘要{i}", (20 + i * 10, 20 + i * 10 + 9), None)
               for i in range(6)]
        s.recent_buffer = [
            HumanMessage(content=f"[{lv1} · L1 · 第0-19条] {NODE_MARKER}\n[摘要] 更早的父节点",
                         metadata={"node_id": lv1, "node_start": 0}),
        ] + [
            HumanMessage(content=f"[{nid} · L0 · 第{20+i*10}-{29+i*10}条] {NODE_MARKER}\n[摘要] 摘要{i}",
                         metadata={"node_id": nid, "node_start": 20 + i * 10})
            for i, nid in enumerate(ids)
        ]
        comp = SummarizationMiddleware(s)
        captured = []

        def fake(ctx, instruction):
            captured.append([str(m.content) for m in ctx])
            return MemoryNodeOutput(summary="合并摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        comp.compress_snapshot(list(s.recent_buffer))
        ctx = captured[0]
        assert len(ctx) == 4, f"应只带：更早的父节点 + 目标 3 个，实际 {len(ctx)}"
        assert any("更早的父节点" in c for c in ctx), "目标之前的节点应保留（背景）"
        assert not any("摘要3" in c or "摘要4" in c or "摘要5" in c for c in ctx), \
            "目标之后的节点（未来）不得进输入"
        assert sum(1 for c in ctx if NODE_MARKER not in c) == 3, "3 个目标去标记"
    finally:
        s.close()


def test_compact_removes_fresh_leaf_messages_from_adds(data_root, monkeypatch):
    """层级合并的目标包含**本轮刚产生的分片叶子**时，其消息必须从 adds 摘除——
    否则 inactive 的子节点消息会和父节点并存于 buffer（历史 bug：合并后 L0 与
    父节点同时可见、L0 数超过阈值、范围看起来重叠/不连续——用户实测 2026-10-19）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 5,
                           "compress_chunk_tokens": 10000,
                           "compact_threshold": 5, "compact_factor": 4})
        tree = s.get_memory_tree()
        old_ids = [tree.add_leaf(f"老{i}", (i * 10, i * 10 + 9), None) for i in range(3)]
        s.recent_buffer = [
            HumanMessage(content=f"[{nid} · L0 · 第{i*10}-{i*10+9}条] {NODE_MARKER}\n[摘要] 老{i}",
                         metadata={"node_id": nid, "node_start": i * 10})
            for i, nid in enumerate(old_ids)
        ]
        msgs = list(s.recent_buffer) + [
            HumanMessage(content=f"m{i:03d}" + "x" * 999,
                         metadata={"ts": "2026-08-08 10:00:00"})
            for i in range(60)]
        comp = SummarizationMiddleware(s)

        def fake(ctx, instruction):
            return MemoryNodeOutput(summary="摘要")

        monkeypatch.setattr(comp, "_call_compress", fake)
        removes, adds = comp.compress_snapshot(msgs)
        adds_ids = [(getattr(m, "metadata", None) or {}).get("node_id") for m in adds]
        status = tree.get_node_status([i for i in adds_ids if i])
        stale = [i for i in adds_ids
                 if i and not (status.get(i) or {}).get("is_active", True)]
        assert not stale, f"adds 里不得有 inactive（已被合并）的节点消息: {stale}"
        assert tree.get_level_count(0) < 5, "层级合并应消化到低于阈值"
        assert tree.get_level_count(1) >= 1, "应产生父节点"
    finally:
        s.close()


def test_drop_superseded_node_messages(data_root):
    """启动/压缩后的自愈：清除 buffer 里"已被父节点取代"的旧节点消息；
    父节点消息缺失时保留（宁可重复，不丢覆盖）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        tree = s.get_memory_tree()
        kids = [tree.add_leaf(f"子{i}", (i * 10, i * 10 + 9), None) for i in range(2)]
        parent = tree.compact(kids, "父")
        parent_msg = HumanMessage(
            content=f"[{parent} · L1 · 第0-19条] {NODE_MARKER}\n[摘要] 父",
            metadata={"node_id": parent, "node_start": 0})
        kid_msgs = [
            HumanMessage(content=f"[{nid} · L0 · 第{i*10}-{i*10+9}条] {NODE_MARKER}\n[摘要] 子{i}",
                         metadata={"node_id": nid, "node_start": i * 10})
            for i, nid in enumerate(kids)]
        raw = HumanMessage(content="原始消息", metadata={"ts": "2026-08-08 10:00:00"})
        # 场景 1：父节点消息在场 → 子消息被清
        s.recent_buffer = [parent_msg] + kid_msgs + [raw]
        removed = s._drop_superseded_node_messages()
        assert removed == 2
        ids = [(getattr(m, "metadata", None) or {}).get("node_id") for m in s.recent_buffer]
        assert parent in ids and not any(k in ids for k in kids)
        # 场景 2：父节点消息缺失 → 子消息保留（宁可重复不丢覆盖）
        s.recent_buffer = list(kid_msgs)
        removed2 = s._drop_superseded_node_messages()
        assert removed2 == 0
        assert len(s.recent_buffer) == 2
    finally:
        s.close()
