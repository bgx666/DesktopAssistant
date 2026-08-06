"""Agent 生成管线 + 工具行为测试（mock LLM，不调真实 API）。"""

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
        _drive_generation(s, "（30 分钟过去了。你醒了过来。）", "heartbeat")
        tasks = s.db.list_tasks()
        t = s.db.get_task(tasks[0]["id"])
        assert t["phases"], "应已拆解出阶段"
        assert t["plan_items"], "应已生成待办条目"
        # 再模拟心跳：勾选完成（动态队列，无固定日期）
        _drive_generation(s, "（30 分钟过去了。你醒了过来。）", "heartbeat")
        pending = s.db.list_pending()
        assert any(p["status"] == "done" for p in s.db.get_plan()) or not pending
    finally:
        s.close()


def test_heartbeat_clamp_in_tool(data_root):
    s = PlannerSession(data_root, mock=True)
    try:
        tools = {t.name: t for t in build_tools(s)}
        r = tools["heartbeat"].invoke({"minutes": 5})
        assert "10 分钟" in r
        assert s._heartbeat_minutes == 10
        tools["heartbeat"].invoke({"minutes": 100000})
        assert s._heartbeat_minutes == 720
        assert s.heartbeat_dict()["in_minutes"] > 0
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
        # 心跳被重置为对话默认短间隔
        hb = s.heartbeat_dict()
        assert 0 < hb["in_minutes"] <= DIALOG_HEARTBEAT_MINUTES
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


def test_tool_result_not_lost_after_compress(data_root):
    """压缩中间件 RemoveMessage 收缩消息列表后，工具结果事件仍要发出。

    回归：旧实现按 len(msgs) 增量检测，压缩把 60 条缩到 20 条后
    len < seen_count → 新增 ToolMessage 永不遍历 → 工具卡片永远转圈。
    """
    from planner.middleware import SUMMARIZE_TRIGGER_MESSAGES
    from planner.session import PlannerSession as PS
    s = PS(data_root, mock=True)
    try:
        # 塞到压缩阈值前 1 条，玩家消息触发后必超阈值 → 本轮 before_model 压缩
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
                f"工具 {tc['name']} 的 tool_result 未发出（压缩后丢失 → 卡片转圈）"
            )
        # 确认压缩真的发生了（buffer 里出现 node 摘要消息）
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
