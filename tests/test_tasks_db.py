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
    db.add_plan_item(tid, None, "2026-08-05", 0, "今天的任务")
    today = "2026-08-05"
    s = db.summary(today)
    assert s["today_plan_total"] == 1
    assert s["today_plan_done"] == 0
    assert len(s["overdue"]) == 1
    assert s["overdue"][0]["content"] == "早该做完的事"
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
