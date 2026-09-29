"""CalendarDb（日历库）测试：存储层 + 迁移 + 重复系列。

历史：本文件原为 test_tasks_db.py。任务/阶段/日计划/回访模块已于 2026-09-12
整体移除（规划统一走日历），故只保留与日历相关的用例。
"""

from planner.store.calendar_db import CalendarDb


def test_calendar_crud_basics(data_root):
    db = CalendarDb(data_root / "planner.db")
    i1 = db.add_calendar_item("2026-09-11", "改论文", "20:00")
    i2 = db.add_calendar_item("2026-09-11", "买牛奶", None, kind="task")
    assert (i1, i2) == (1, 2), "新增应返回自增 id"

    items = db.list_calendar_range("2026-09-11", "2026-09-11")
    assert [i["id"] for i in items] == [i2, i1], "全天项排当天最前"
    assert items[1]["time"] == "20:00" and items[0]["time"] is None

    assert db.update_calendar_item(i1, time="20:30", content="改论文（二稿）") is True
    got = db.get_calendar_item(i1)
    assert got["time"] == "20:30" and got["content"] == "改论文（二稿）"

    assert db.set_calendar_status(i1, "done") is True
    assert db.get_calendar_item(i1)["done_at"], "标完成应写 done_at"
    assert db.set_calendar_status(i1, "todo") is True
    assert db.get_calendar_item(i1)["done_at"] is None, "撤销完成应清掉 done_at"

    assert db.cancel_calendar_item(i2) is True
    left = db.list_calendar_range("2026-09-11", "2026-09-11")
    assert [i["id"] for i in left] == [i1], "取消的项默认不再返回，其它项不受影响"
    assert len(db.list_calendar_range("2026-09-11", "2026-09-11",
                                      include_cancelled=True)) == 2
    db.close()


def test_exec_write_returns_rowcount_not_lastrowid(data_root):
    """`_exec(mode="write")`：INSERT 返回新 id，UPDATE/DELETE 返回受影响行数。

    回归：原来统一返回 `cur.lastrowid or cur.rowcount`，而 sqlite3 里
    UPDATE/DELETE 的 lastrowid 会**残留上一次 INSERT 的 rowid** —— 于是
    "改 0 行"也返回非 0，`> 0` 判断恒为真，改什么都没动也会报告成功。
    """
    db = CalendarDb(data_root / "planner.db")
    a = db.add_calendar_item("2026-09-11", "A")
    b = db.add_calendar_item("2026-09-12", "B")
    assert (a, b) == (1, 2), "INSERT 仍必须返回新 id"

    assert db._exec("UPDATE calendar_items SET content = 'X' WHERE id = 999",
                    mode="write") == 0
    assert db._exec("UPDATE calendar_items SET content = 'Y' WHERE id = ?",
                    (a,), mode="write") == 1
    assert db._exec("DELETE FROM calendar_items WHERE id = 999", mode="write") == 0
    assert db._exec("DELETE FROM calendar_items WHERE id = ?", (b,), mode="write") == 1

    # 业务方法不再误报成功
    assert db.update_calendar_item(9999, content="Z") is False
    assert db.set_calendar_status(9999, "done") is False
    assert db.cancel_calendar_item(9999) is False
    assert db.update_calendar_item(a, content="真的改了") is True
    db.close()


def test_calendar_series_dates_and_extend(data_root):
    """重复系列：日期算得对、续期不漂移星期几、且幂等。"""
    from datetime import date

    db = CalendarDb(data_root / "planner.db")

    sid, n = db.add_calendar_series("2026-09-16", "组会", "weekly", "20:00")
    assert n == 53 and sid
    days = [r["date"] for r in db.list_calendar_range("2026-09-16", "2026-12-31")
            if r["series_id"] == sid]
    assert days[0] == "2026-09-16" and all(
        date.fromisoformat(d).weekday() == 2 for d in days), "每周三不能漂移"

    wid, k = db.add_calendar_series("2026-09-14", "站会", "weekdays", "09:30")
    assert k > 0 and all(
        date.fromisoformat(r["date"]).weekday() < 5
        for r in db.list_calendar_range("2026-09-14", "2026-11-30")
        if r["series_id"] == wid), "工作日不应含周末"

    # 站在 2026-12-01：weekly 系列已排到 2027-09-15，还够 → 不需要续
    assert sid not in db.list_calendar_series_need_extend("2026-12-01")
    # 把它的 horizon 拉回过去，模拟"快用完了"
    db._exec("UPDATE calendar_items SET repeat_until = '2026-12-30' WHERE series_id = ?",
             (sid,), mode="write")
    assert sid in db.list_calendar_series_need_extend("2026-12-01")
    added = db.extend_calendar_series(sid, today="2026-12-01")
    assert added > 0
    tail = [r["date"] for r in db.list_calendar_range("2026-12-31", "2027-12-01")
            if r["series_id"] == sid]
    assert tail and all(date.fromisoformat(d).weekday() == 2 for d in tail), \
        "续期后仍必须是周三（base 用最初起始日，不能用续期起点）"
    # 幂等：再续一次不新增
    assert db.extend_calendar_series(sid, today="2026-12-01") == 0
    db.close()


def test_legacy_task_tables_are_dropped(data_root):
    """已移除模块的四张表在打开时被 DROP（master 要求不留历史残留）。"""
    import sqlite3

    p = data_root / "planner.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE phases (id INTEGER PRIMARY KEY, task_id INTEGER, title TEXT);
        CREATE TABLE plan_items (id INTEGER PRIMARY KEY, task_id INTEGER, content TEXT);
        CREATE TABLE reviews (id INTEGER PRIMARY KEY, task_id INTEGER, summary TEXT);
        INSERT INTO tasks (title) VALUES ('旧任务');
    """)
    c.commit()
    c.close()
    assert "tasks" in [r[0] for r in
                       sqlite3.connect(p).execute(
                           "select name from sqlite_master where type='table'")]

    db = CalendarDb(p)
    tables = [r[0] for r in sqlite3.connect(p).execute(
        "select name from sqlite_master where type='table'")]
    assert "tasks" not in tables and "plan_items" not in tables
    assert "phases" not in tables and "reviews" not in tables
    # 日历表照常可用
    assert db.add_calendar_item("2026-09-11", "迁移后仍可用")
    db.close()
