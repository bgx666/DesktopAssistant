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
        # 事件队列里有文本
        texts = [e for e in s.drain_events() if e["type"] == "text"]
        assert texts
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
        assert t["plan_items"], "应已生成日计划"
        # 再模拟心跳：勾选完成
        _drive_generation(s, "（30 分钟过去了。你醒了过来。）", "heartbeat")
        plan = s.db.get_today_plan()
        assert plan and any(p["status"] == "done" for p in plan)
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


def test_dnd_window_check(data_root):
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
        assert info == {"details": []}
    finally:
        s.close()
