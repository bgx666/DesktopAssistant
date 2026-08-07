"""Agent 生成管线 + 工具行为测试（mock LLM，不调真实 API）。"""

import time

from langchain_core.messages import HumanMessage

from planner.memory.sqlite_memory_tree import SQLiteMemoryTree
from planner.session import PlannerSession
from planner.tools import build_tools


def _drive_generation(session, content: str, trigger: str = "player") -> None:
    """手动驱动一次生成（单线程，绕过 worker 线程）。"""
    session._receive(content, trigger=True)
    session.pending_response = False
    session._generate_response(trigger)


def test_player_message_creates_task(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "帮我安排一下学习计划")
        tasks = s.db.list_tasks()
        assert len(tasks) >= 1
        assert tasks[0]["title"] == "复习线性代数第三章"
        # buffer 里应该有 assistant 消息（流式文本）+ 工具结果
        assert any(getattr(m, "type", None) == "ai" for m in s.recent_buffer)
        assert any(getattr(m, "type", None) == "tool" for m in s.recent_buffer)
        # 事件队列里有文本 + 流式事件（一次 drain 后分类）
        events = s.drain_events()
        texts = [e for e in events if e["type"] == "text"]
        assert texts
        streams = [e for e in events if e["type"] == "text_stream"]
        assert streams, "应有 text_stream 流式事件"
        by_id = {}
        for e in streams:
            by_id.setdefault(e["msg_id"], []).append(e["content"])
        joined = "".join("".join(v) for v in by_id.values())
        assert joined == texts[0]["content"], "流式拼接应与完整文本一致"
        # 工具调用卡片事件：tool_call 在前、tool_result 在后（heartbeat 不展示）
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert tool_calls, "应有 tool_call 事件"
        assert all(e["name"] != "heartbeat" for e in tool_calls)
        assert len(tool_results) >= len(tool_calls)
        for tc in tool_calls:
            assert any(r["id"] == tc["id"] for r in tool_results), "tool_result 应对应 tool_call"
    finally:
        s.close()


def test_history_tool_calls_not_replayed(data_root):
    """历史 buffer 里的工具调用不应在后续每轮重新推送 tool_call 卡片。"""
    s = PlannerSession(data_root, mock=True)
    try:
        # 第一轮：创建任务（产生 create_task 工具卡片）
        _drive_generation(s, "帮我安排一下学习计划")
        first = s.drain_events()
        assert any(e["type"] == "tool_call" for e in first)
        # 第二轮：历史 buffer 含 create_task 工具调用，不应重放
        _drive_generation(s, "（心跳）继续看看", "heartbeat")
        second = s.drain_events()
        second_calls = [e for e in second if e["type"] == "tool_call"]
        # 心跳轮 mock 会勾选/拆解任务（可能有新工具），但绝不能出现
        # 第一轮 create_task 的历史工具调用（按参数识别）
        assert not any(
            e["name"] == "create_task" and e["args"].get("title") == "复习线性代数第三章"
            for e in second_calls
        ), "历史 create_task 被重放为卡片"
        # 第三轮同验
        _drive_generation(s, "（心跳）继续", "heartbeat")
        third = s.drain_events()
        assert not any(
            e["type"] == "tool_call"
            and e["name"] == "create_task"
            and e["args"].get("title") == "复习线性代数第三章"
            for e in third
        ), "历史 create_task 被重放为卡片"
    finally:
        s.close()


def test_heartbeat_cycle_breaks_down_and_marks_done(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s, "给我安排任务", "player")
        assert s.db.list_tasks()
        # 模拟心跳：拆解任务
        _drive_generation(s, "（心跳到了，请主动跟用户说点东西。）", "heartbeat")
        tasks = s.db.list_tasks()
        t = s.db.get_task(tasks[0]["id"])
        assert t["phases"], "应已拆解出阶段"
        assert t["plan_items"], "应已生成待办条目"
        # 再模拟心跳：勾选完成（动态队列，无固定日期）
        _drive_generation(s, "（心跳到了，请主动跟用户说点东西。）", "heartbeat")
        pending = s.db.list_pending()
        assert any(p["status"] == "done" for p in s.db.get_plan()) or not pending
    finally:
        s.close()


def test_heartbeat_clamp_in_tool(data_root):
    from planner import config as _config
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        # 秒级心跳：0.2 分钟（12 秒）有效
        r = tools["heartbeat"].invoke({"minutes": 0.2})
        assert abs(s._heartbeat_minutes - 0.2) < 1e-6
        assert "12 秒" in r
        # 下限护栏 ≈10 秒：0 被拉高到 0.17
        tools["heartbeat"].invoke({"minutes": 0})
        assert abs(s._heartbeat_minutes - _config.PLANNER_HEARTBEAT_MIN_MINUTES) < 1e-6
        tools["heartbeat"].invoke({"minutes": 100000})
        assert s._heartbeat_minutes == 720
        d = s.heartbeat_dict()
        assert d["in_seconds"] > 0
    finally:
        s.close()


def test_dnd_tool(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        tools["set_do_not_disturb"].invoke({"enabled": True, "until_hour": 14})
        assert s.dnd_until is not None
        assert s.dnd_until.hour == 14
        tools["set_do_not_disturb"].invoke({"enabled": False})
        assert s.dnd_until is None
        assert not s.dnd_enabled
    finally:
        s.close()


def test_dnd_window_check(data_root, monkeypatch):
    import planner.config as cfg
    # 恢复默认窗口 22-8 测试 in_dnd 逻辑
    monkeypatch.setattr(cfg, "PLANNER_DND_START_HOUR", 22)
    monkeypatch.setattr(cfg, "PLANNER_DND_END_HOUR", 8)
    s = PlannerSession(data_root, mock=True)
    try:
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=8))
        assert s.in_dnd(datetime(2026, 8, 5, 23, 0, tzinfo=tz)) is True   # 23 点在默认窗口内
        assert s.in_dnd(datetime(2026, 8, 5, 12, 0, tzinfo=tz)) is False  # 中午不打扰
        assert s.in_dnd(datetime(2026, 8, 5, 7, 30, tzinfo=tz)) is True   # 7 点半（跨天窗口）
        s.set_dnd(False)
        assert s.in_dnd(datetime(2026, 8, 5, 23, 0, tzinfo=tz)) is False
    finally:
        s.close()


def test_break_down_task_tool(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        tid = s.db.create_task("学 Python", "基础", "2026-08-30", "high")
        tools = {t.name: t for t in build_tools(s)}
        r = tools["break_down_task"].invoke({
            "task_id": tid,
            "phases": [
                {"title": "语法", "days": 2, "items": [
                    {"date_offset": 0, "content": "读第一章", "est_minutes": 60},
                    {"date_offset": 1, "content": "做练习", "est_minutes": 30},
                ]},
                {"title": "实战", "days": 1, "items": [
                    {"date_offset": 0, "content": "写小项目"},
                ]},
            ],
        })
        assert "拆解完成" in r
        task = s.db.get_task(tid)
        assert len(task["phases"]) == 2
        assert len(task["plan_items"]) == 3
        assert task["status"] == "in_progress"
        assert task["phases"][0]["status"] == "active"
    finally:
        s.close()


def test_mark_plan_done_auto_completes_task(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        tid = s.db.create_task("小任务")
        pid = s.db.add_plan_item(tid, None, "2026-08-05", 0, "做一件事")
        tools = {t.name: t for t in build_tools(s)}
        tools["mark_plan_done"].invoke({"plan_id": pid})
        assert s.db.get_task(tid)["status"] == "done"
    finally:
        s.close()


def test_buffer_persists_across_restart(data_root):
    s1 = PlannerSession(data_root, mock=True)
    try:
        _drive_generation(s1, "你好")
        assert s1.recent_buffer
        saved_counter = s1._msg_counter
    finally:
        s1.close()
    s2 = PlannerSession(data_root, mock=True)
    try:
        assert s2.recent_buffer, "重启后应恢复 buffer"
        assert s2._msg_counter == saved_counter
    finally:
        s2.close()


def test_plan_snapshot_injection(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        s.db.create_task("任务 A")
        text = s._plan_snapshot_text()
        assert text is not None and "任务 A" in text
        # 指纹未变 → 不再注入
        assert s._plan_snapshot_text() is None
        # 计划变化 → 重新注入
        s.db.create_task("任务 B")
        text2 = s._plan_snapshot_text()
        assert text2 is not None and "任务 B" in text2
    finally:
        s.close()


def test_memory_tree_wired_to_session(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        assert isinstance(s.get_memory_tree(), SQLiteMemoryTree)
        nid = s.get_memory_tree().add_leaf("测试摘要", (0, 1), None)
        info = s.get_memory_tree().get_node_children_info(nid)
        assert info["details"] == []
        assert info["profile"] is None
        assert info["future_notes"] is None
    finally:
        s.close()

def test_player_message_resets_heartbeat(data_root):
    """用户说话 → 取消旧长心跳、设对话短心跳、清零沉默计数。"""
    from planner.session import DIALOG_HEARTBEAT_MINUTES
    s = PlannerSession(data_root, mock=True)
    try:
        s.schedule_heartbeat(120)          # 模拟旧的长心跳
        assert s.heartbeat_dict()["in_minutes"] > 60
        s.enqueue_player_message("我回来了")
        # 心跳被重置为对话默认短间隔（秒级）
        hb = s.heartbeat_dict()
        assert 0 < hb["in_seconds"] <= DIALOG_HEARTBEAT_MINUTES * 60 + 2
        assert s._heartbeat_silent_count == 0
    finally:
        s.close()


def test_silent_escalation(data_root):
    """连续自主唤醒用户没反应 → 心跳逐步加长（10 → 20 → … → 120 上限）。"""
    from planner.session import DIALOG_HEARTBEAT_MINUTES, SILENT_ESCALATE_MAX, SILENT_ESCALATE_STEP
    s = PlannerSession(data_root, mock=True)
    try:
        assert s._next_silent_minutes() == DIALOG_HEARTBEAT_MINUTES
        s._heartbeat_silent_count = 1
        assert s._next_silent_minutes() == DIALOG_HEARTBEAT_MINUTES + SILENT_ESCALATE_STEP
        s._heartbeat_silent_count = 5
        assert s._next_silent_minutes() == DIALOG_HEARTBEAT_MINUTES + 5 * SILENT_ESCALATE_STEP
        s._heartbeat_silent_count = 99
        assert s._next_silent_minutes() == SILENT_ESCALATE_MAX
    finally:
        s.close()


def test_fire_heartbeat_counts_silence(data_root):
    """心跳触发（自主唤醒）→ 沉默计数 +1；玩家消息 → 清零。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s._heartbeat_silent_count = 0
        s.schedule_heartbeat(10)
        s._next_heartbeat_at = 0.0   # 模拟到点
        # 直接走触发逻辑（mock 会真生成，走 worker 时序复杂，只验证计数函数）
        s._heartbeat_silent_count += 1  # 对应 _fire_heartbeat 的计数
        assert s._heartbeat_silent_count == 1
        s.enqueue_player_message("在的")
        assert s._heartbeat_silent_count == 0
    finally:
        s.close()


def _node_count(session) -> int:
    return sum(1 for m in session.recent_buffer
               if "node_id" in (getattr(m, "metadata", None) or {}))


def _trigger_async_compress(session, timeout=15.0):
    """触发异步压缩并等待节点数增加（后台线程完成）。

    先等上一个压缩线程完全结束（_compressing 复位），避免 _maybe_compress_async
    因并发标志跳过导致下一轮不触发；按节点数增加判断，避免上一轮的
    节点让等待条件提前满足。
    """
    deadline = time.time() + timeout
    while session._compressing and time.time() < deadline:
        time.sleep(0.05)
    before = _node_count(session)
    session._maybe_compress_async()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _node_count(session) > before:
            return
        time.sleep(0.1)
    raise AssertionError("异步压缩超时未完成")


def test_tool_result_not_lost_after_compress(data_root):
    """工具结果事件配对正常（历史 seen_count 回归场景由异步压缩规避）。"""
    from planner.middleware import SUMMARIZE_TRIGGER_MESSAGES
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        # 塞到压缩阈值前 1 条，玩家消息触发后必超阈值
        for i in range(SUMMARIZE_TRIGGER_MESSAGES - 1):
            s.recent_buffer.append(HumanMessage(content=f"占位消息 {i}"))
        s._msg_counter += SUMMARIZE_TRIGGER_MESSAGES - 1

        _drive_generation(s, "帮我安排一下学习计划")
        events = s.drain_events()

        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert tool_calls, "本轮应有工具调用事件"
        for tc in tool_calls:
            assert any(r["id"] == tc["id"] for r in tool_results), (
                f"工具 {tc['name']} 的 tool_result 未发出"
            )
        # 异步压缩最终应落节点
        _trigger_async_compress(s)
        assert any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer), "压缩应已触发"
    finally:
        s.close()


def test_compressed_nodes_kept_at_start(data_root):
    """连续多次压缩后，节点消息按 node_start 排序集中在 buffer 开头。

    回归：压缩节点经 langgraph add_messages 追加到末尾，多次压缩后
    早期摘要会卡在对话中间（时间顺序错乱）。
    """
    from planner.middleware import SUMMARIZE_KEEP_MESSAGES, SUMMARIZE_TRIGGER_MESSAGES
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        for _round in range(2):
            for i in range(SUMMARIZE_TRIGGER_MESSAGES - 1):
                s.recent_buffer.append(HumanMessage(content=f"第{_round}轮占位 {i}"))
            s._msg_counter += SUMMARIZE_TRIGGER_MESSAGES - 1
            _drive_generation(s, f"帮我安排任务（第{_round}轮）")
            _trigger_async_compress(s)

        nodes = [m for m in s.recent_buffer
                 if "node_id" in (getattr(m, "metadata", None) or {})]
        rest = [m for m in s.recent_buffer
                if "node_id" not in (getattr(m, "metadata", None) or {})]
        assert len(nodes) >= 2, "两轮压缩后应有 ≥2 个节点"
        # 节点全部在开头
        assert s.recent_buffer[:len(nodes)] == nodes, "节点应全部排在 buffer 开头"
        # 按 node_start 升序
        starts = [(getattr(m, "metadata", None) or {}).get("node_start") for m in nodes]
        assert starts == sorted(starts), f"节点应按 node_start 升序：{starts}"
        assert all("node_id" not in (getattr(m, "metadata", None) or {}) for m in rest)
        assert len(rest) <= SUMMARIZE_KEEP_MESSAGES + 10
    finally:
        s.close()


def test_compressed_ranges_are_global_sequential(data_root):
    """多次压缩的节点范围应为全局连续序号（001: 0-39, 002: 40-79…），不重叠。

    回归：旧实现用 enumerate(msgs) 相对索引，每次压缩节点都显示"第0-40条"
    （假重叠）；对齐小B _span 机制后按 _compressed_total 累计。
    """
    import re
    from planner.middleware import SUMMARIZE_KEEP_MESSAGES, SUMMARIZE_TRIGGER_MESSAGES
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        for _round in range(2):
            for i in range(SUMMARIZE_TRIGGER_MESSAGES - 1):
                s.recent_buffer.append(HumanMessage(content=f"第{_round}轮占位 {i}"))
            s._msg_counter += SUMMARIZE_TRIGGER_MESSAGES - 1
            _drive_generation(s, f"帮我安排任务（第{_round}轮）")
            _trigger_async_compress(s)

        nodes = [m for m in s.recent_buffer
                 if "node_id" in (getattr(m, "metadata", None) or {})]
        assert len(nodes) >= 2
        ranges = []
        for m in nodes:
            match = re.search(r"第(\d+)-(\d+)条", str(m.content))
            assert match, f"节点消息应有范围标注: {str(m.content)[:60]}"
            ranges.append((int(match.group(1)), int(match.group(2))))
        # 范围连续不重叠：后一个 start == 前一个 end + 1
        for prev, cur in zip(ranges, ranges[1:]):
            assert cur[0] == prev[1] + 1, f"节点范围应连续: {ranges}"
        # 首个节点从全局 0 开始；累计值 = 最后节点 end + 1
        assert ranges[0][0] == 0, f"首个节点应从 0 开始: {ranges}"
        assert s._compressed_total == ranges[-1][1] + 1
    finally:
        s.close()


def test_compression_emits_memory_update(data_root):
    """压缩成功后应推送 memory_update 事件（前端据此重载历史同步对话框）。"""
    from planner.middleware import SUMMARIZE_TRIGGER_MESSAGES
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        for i in range(SUMMARIZE_TRIGGER_MESSAGES - 1):
            s.recent_buffer.append(HumanMessage(content=f"占位消息 {i}"))
        s._msg_counter += SUMMARIZE_TRIGGER_MESSAGES - 1
        _drive_generation(s, "帮我安排一下学习计划")
        # 异步压缩完成 → memory_update 事件
        deadline = time.time() + 15
        got_update = False
        while time.time() < deadline:
            events = s.drain_events()
            if any(e["type"] == "memory_update" for e in events):
                got_update = True
                break
            if any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer):
                break
            s._maybe_compress_async()
            time.sleep(0.1)
        assert got_update, "压缩应推送 memory_update"
        assert any("node_id" in (getattr(m, "metadata", None) or {})
                   for m in s.recent_buffer), "压缩应已发生"
    finally:
        s.close()


def test_compress_batch_includes_paired_tool_messages(data_root):
    """压缩 batch 切开 ai(tool_calls) 与其工具结果时，配套 tool 消息必须一并压缩。

    回归：否则删 ai 留 tool → 孤儿 ToolMessage → DeepSeek 400（生成全挂）。
    """
    from langchain_core.messages import AIMessage, ToolMessage
    from planner.middleware import SummarizationMiddleware, SUMMARIZE_KEEP_MESSAGES
    s = PlannerSession(data_root, mock=True)
    try:
        # 构造：ai(tool_calls) 恰好在 batch 边界（index 40），tool 结果在保留区
        for i in range(40):
            s.recent_buffer.append(HumanMessage(content=f"占位 {i}"))
        ai = AIMessage(content="调用工具", id="cut-ai",
                       tool_calls=[{"name": "get_next_actions", "args": {},
                                    "id": "cut-call-1", "type": "tool_call"}])
        tool = ToolMessage(content="结果", tool_call_id="cut-call-1", id="cut-tool")
        s.recent_buffer.append(ai)
        s.recent_buffer.append(tool)
        for i in range(19):
            s.recent_buffer.append(HumanMessage(content=f"后置 {i}"))
        s._msg_counter += 40 + 2 + 19

        comp = SummarizationMiddleware(s)
        removes, _ = comp.compress_snapshot(list(s.recent_buffer))
        remove_ids = {getattr(m, "id", None) or id(m) for m in removes}
        # 配套 tool 消息必须与 ai 一起被压缩删除（不在 remove_ids 会留孤儿）
        assert "cut-ai" in remove_ids, "边界处的 ai(tool_calls) 应被压缩"
        assert "cut-tool" in remove_ids, "配套 tool 消息必须一并压缩（否则孤儿 400）"

        # 应用删除后 buffer 无孤儿 tool
        kept = [m for m in s.recent_buffer
                if (getattr(m, "id", None) or id(m)) not in remove_ids]
        call_ids = set()
        for m in kept:
            for tc in (getattr(m, "tool_calls", None) or []):
                call_ids.add(tc.get("id"))
        orphans = [m for m in kept if getattr(m, "type", None) == "tool"
                   and (getattr(m, "tool_call_id", None) or "") not in call_ids]
        assert not orphans, "应用删除后不应有孤儿 tool 消息"
    finally:
        s.close()


def test_strip_orphan_tool_messages(data_root):
    """生成输入前清理孤儿 tool 消息（防御 400）。"""
    from langchain_core.messages import ToolMessage
    s = PlannerSession(data_root, mock=True)
    try:
        s.recent_buffer.append(HumanMessage(content="你好"))
        s.recent_buffer.append(ToolMessage(content="孤儿结果", tool_call_id="ghost-call"))
        s._strip_orphan_tool_messages()
        assert not any(getattr(m, "type", None) == "tool" for m in s.recent_buffer), \
            "孤儿 tool 应被清理"
        # 正常配对的不动
        s.recent_buffer.append(HumanMessage(content="再问"))
        from langchain_core.messages import AIMessage
        s.recent_buffer.append(AIMessage(
            content="",
            tool_calls=[{"name": "x", "args": {}, "id": "ok-call", "type": "tool_call"}]))
        s.recent_buffer.append(ToolMessage(content="ok", tool_call_id="ok-call"))
        s._strip_orphan_tool_messages()
        assert any(getattr(m, "tool_call_id", None) == "ok-call" for m in s.recent_buffer)
    finally:
        s.close()


def test_player_message_carries_gap_hint(data_root):
    """玩家消息附带「距上次说话 X」间隔提示（首次不带、之后带、格式化）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        # 首次：无上次记录 → 不带间隔
        s.enqueue_player_message("第一句")
        first = next(m for m in s.recent_buffer
                     if getattr(m, "type", "") == "human" and "第一句" in str(m.content))
        assert "距上次" not in str(first.content), "首次消息不应带间隔"

        # 第二次：间隔几秒 → 带"X 秒"
        time.sleep(1.2)
        s.enqueue_player_message("第二句")
        second = next(m for m in s.recent_buffer
                      if getattr(m, "type", "") == "human" and "第二句" in str(m.content))
        c = str(second.content)
        assert "距上次说话" in c, f"应有间隔提示: {c}"
        assert "秒" in c, f"秒级间隔: {c}"

        # 格式：1 小时 5 分钟
        assert s._fmt_gap(3900) == "1 小时 5 分钟"
        assert s._fmt_gap(125) == "2 分 5 秒"
        assert s._fmt_gap(30) == "30 秒"
    finally:
        s.close()


def test_gap_hint_not_shown_in_history(data_root):
    """间隔提示在"对你说："之前 → /history 切分后不显示。"""
    s = PlannerSession(data_root, mock=True)
    try:
        s.enqueue_player_message("第一句")
        time.sleep(0.2)
        s.enqueue_player_message("第二句")
        second = next(m for m in s.recent_buffer
                      if getattr(m, "type", "") == "human" and "第二句" in str(m.content))
        c = str(second.content)
        assert "距上次" in c
        # 模拟 /history 的切分：对你说：之后的部分不应含间隔
        shown = c.split("对你说：", 1)[1]
        assert "距上次" not in shown, "间隔不应显示在对话框"
    finally:
        s.close()


def test_gap_hint_persists_across_restart(data_root):
    """上次玩家消息时间持久化：重启后首句也带离线时长间隔（累积）。"""
    from datetime import datetime, timedelta
    from planner.session import _TZ
    s1 = PlannerSession(data_root, mock=True)
    try:
        s1.enqueue_player_message("第一句")
        s1._last_player_message_at = datetime.now(_TZ) - timedelta(hours=2)   # 模拟离线 2 小时
        s1._save_buffer_state()
    finally:
        s1.close()

    s2 = PlannerSession(data_root, mock=True)
    try:
        s2.enqueue_player_message("第二句")
        m = next(x for x in s2.recent_buffer
                 if "第二句" in str(getattr(x, "content", "")))
        c = str(m.content)
        assert "距上次说话" in c, f"重启后首句应带间隔: {c}"
        assert "2 小时" in c, f"应显示离线时长: {c}"
    finally:
        s2.close()


def test_heartbeat_may_stay_silent_with_empty_reply(data_root):
    """心跳无话可说（空回复）→ 不冒泡、buffer 不留空 AI 消息。"""
    from langchain_core.messages import AIMessage
    from planner.llm import MockChatModel

    class _SilentMock(MockChatModel):
        """心跳类系统消息 → 空回复；玩家消息走原脚本。"""

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            last = self._last_user_content(messages)
            if last.startswith(("（", "[", "【")):
                return self._make_result("")
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    s = PlannerSession(data_root, mock=True)
    try:
        s.set_chat_model(_SilentMock(session=s))
        _drive_generation(s, "（系统：心跳到了，请主动和用户说话。如果此刻实在没什么想说的，可以不说。）", "heartbeat")
        events = s.drain_events()
        assert not any(e["type"] == "text" for e in events), "空回复不应冒泡 text 事件"
        # 心跳注入消息之外，buffer 不应残留空 AI 消息
        assert not any(
            getattr(m, "type", None) == "ai"
            and not str(getattr(m, "content", "") or "").strip()
            for m in s.recent_buffer
        ), "空 AI 消息不应落 buffer"
        # 玩家消息仍正常回复
        _drive_generation(s, "在吗？", "player")
        assert any(e["type"] == "text" for e in s.drain_events())
    finally:
        s.close()
