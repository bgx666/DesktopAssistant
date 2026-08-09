"""TasksDb 任务库测试。"""

from planner.store.tasks_db import TasksDb


def test_create_and_get_task(data_root):
    db = TasksDb(data_root / "planner.db")
    tid = db.create_task("完成毕设", "论文与答辩", "2026-08-20", "high")
    t = db.get_task(tid)
    assert t["title"] == "完成毕设"
    assert t["priority"] == "high"
    assert t["status"] == "todo"
    db.close()


def test_phases_and_plan_items(data_root):
    db = TasksDb(data_root / "planner.db")
    tid = db.create_task("学 Python")
    p1 = db.add_phase(tid, 0, "语法", "基础", 2)
    p2 = db.add_phase(tid, 1, "实战", "", 3)
    db.add_plan_item(tid, p1, "2026-08-05", 0, "读第一章", 60)
    db.add_plan_item(tid, p2, "2026-08-06", 0, "写小项目", 120)
    plan = db.get_plan(date_="2026-08-05")
    assert len(plan) == 1
    assert plan[0]["content"] == "读第一章"
    assert plan[0]["task_title"] == "学 Python"
    assert plan[0]["phase_title"] == "语法"
    db.close()


def test_mark_plan_done(data_root):
    db = TasksDb(data_root / "planner.db")
    tid = db.create_task("任务")
    pid = db.add_plan_item(tid, None, "2026-08-05", 0, "做某事")
    assert db.set_plan_status(pid, "done") is True
    item = db.get_plan(date_="2026-08-05")[0]
    assert item["status"] == "done"
    assert item["done_at"]
    db.close()


def test_summary_and_overdue(data_root):
    db = TasksDb(data_root / "planner.db")
    tid = db.create_task("过期任务", due_date="2026-08-01")
    db.add_plan_item(tid, None, "2026-08-01", 0, "早该做完的事")
    db.add_plan_item(tid, None, None, 1, "动态待办")
    today = "2026-08-05"
    s = db.summary(today)
    assert s["pending_total"] == 2
    assert s["pending_done"] == 0
    assert len(s["overdue_tasks"]) == 1
    assert s["overdue_tasks"][0]["title"] == "过期任务"
    assert s["queue"][0]["content"] == "早该做完的事" or s["queue"][1]["content"] == "早该做完的事"
    db.close()


def test_update_task_status_adds_review(data_root):
    db = TasksDb(data_root / "planner.db")
    tid = db.create_task("任务")
    assert db.update_task_status(tid, "in_progress", "开始做了") is True
    assert db.get_task(tid)["status"] == "in_progress"
    reviews = db.list_reviews(tid)
    assert len(reviews) == 1
    assert reviews[0]["summary"] == "开始做了"
    assert db.update_task_status(tid, "bad_status") is False
    db.close()


def test_add_days(data_root):
    assert TasksDb.add_days("2026-08-05", 2) == "2026-08-07"
    assert TasksDb.add_days("2026-12-31", 1) == "2027-01-01"


def test_pending_queue_sorted_by_priority_and_due(data_root):
    db = TasksDb(data_root / "planner.db")
    t1 = db.create_task("远期任务", due_date="2026-12-01")
    t2 = db.create_task("紧急任务", due_date="2026-08-06")
    t3 = db.create_task("无截止任务", due_date=None)
    i1 = db.add_plan_item(t1, None, None, 0, "远期待办")
    i2 = db.add_plan_item(t2, None, None, 0, "紧急待办")
    i3 = db.add_plan_item(t3, None, None, 0, "普通待办")
    # 默认：截止日期近的在前
    q = db.list_pending()
    assert [p["id"] for p in q] == [i2, i1, i3]
    # 插队：优先级权重最大的在最前
    db.bump_item_priority(i3)
    q = db.list_pending()
    assert q[0]["id"] == i3
    assert q[0]["priority"] > 0
    db.close()


def test_break_down_items_have_suggested_date(data_root):
    """拆解后待办带建议日期（拆解日 + date_offset）：跨天后 LLM/前端能看出
    日期已过——修复"明晚"这类相对时间表述被当成永远的未来。"""
    from datetime import date, timedelta
    from planner.session import PlannerSession
    from planner.tools import build_tools
    s = PlannerSession(data_root, mock=True)
    try:
        tid = s.db.create_task("学 Python")
        tools = {t.name: t for t in build_tools(s)}
        tools["break_down_task"].invoke({
            "task_id": tid,
            "phases": [{"title": "语法", "days": 2,
                        "items": [{"date_offset": 0, "content": "通读教材"},
                                  {"date_offset": 1, "content": "写练习"}]}],
        })
        items = s.db.get_plan(task_id=tid)
        assert len(items) == 2
        today = date.today().isoformat()
        assert items[0]["date"] == today, "offset=0 → 当天"
        assert items[1]["date"] == (date.today() + timedelta(days=1)).isoformat(), "offset=1 → 明天"
    finally:
        s.close()
