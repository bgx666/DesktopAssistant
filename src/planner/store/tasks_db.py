"""SQLite 任务库：任务 / 阶段 / 日计划 / 回访记录。

数据模型：
- tasks       顶层任务（用户目标）
- phases      任务拆解出的阶段（里程碑），含计划天数
- plan_items  日计划条目（挂在阶段下，date 排期，可独立勾选完成）
- reviews     回访/进度记录（预留，供调度与复盘使用）

所有日期均为 YYYY-MM-DD 字符串（UTC+8 本地日）。

并发：连接可被 handler / worker / 调度线程共用（check_same_thread=False），
所有语句执行与 close() 都经由同一把 RLock 串行化——避免「execute 成功、
另一线程 close、fetchone 返回 None」的竞态（实测修复 2026-08-05）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_PLAN_ITEMS_TABLE = """
CREATE TABLE plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
    date TEXT,
    seq INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    est_minutes INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'todo',
    priority INTEGER NOT NULL DEFAULT 0,
    done_at TEXT
)
"""

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    priority TEXT NOT NULL DEFAULT 'normal',
    due_date TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    days INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
    date TEXT,
    seq INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    est_minutes INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'todo',
    priority INTEGER NOT NULL DEFAULT 0,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    plan_item_id INTEGER,
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    summary TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""

# 旧库（无 priority 列）时，迁移完成后再建该索引
_CREATE_INDEX_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_plan_items_status ON plan_items(status, priority)"
)

_TASK_STATUSES = ("todo", "in_progress", "done", "abandoned")
_PLAN_STATUSES = ("todo", "done", "skipped")


class TasksDb:
    """任务库（单连接，SQLite WAL，RLock 串行化全部访问）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def close(self) -> None:
        """关闭连接。与语句执行互斥：等待在途语句完成后才真正关闭。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _exec(self, sql: str, params: tuple = (), *, mode: str = "one") -> Any:
        """统一执行入口（RLock 内完成 执行+取回/提交 全生命周期）。

        mode: one → 返回单行；all → 返回全部行；write → 自动 commit，返回
        lastrowid（有自增主键时）否则 rowcount。
        """
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError(f"TasksDb 已关闭: {self._db_path}")
            cur = self._conn.execute(sql, params)
            if mode == "one":
                return cur.fetchone()
            if mode == "all":
                return cur.fetchall()
            self._conn.commit()
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def _init_db(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_CREATE_TABLES)
        # 迁移 1：旧库缺 priority 列 → 补列
        row = self._exec("SELECT name FROM pragma_table_info('plan_items') WHERE name = 'priority'")
        if row is None:
            with self._lock, self._conn:
                self._exec("ALTER TABLE plan_items ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        # 迁移 2：旧库 date 列是 NOT NULL（固定日计划）→ 重建表去掉约束（动态待办）
        row = self._exec('SELECT "notnull" FROM pragma_table_info(\'plan_items\') WHERE name = \'date\'')
        if row is not None and row["notnull"] == 1:
            with self._lock, self._conn:
                self._conn.execute("ALTER TABLE plan_items RENAME TO plan_items_old")
                self._conn.execute(_PLAN_ITEMS_TABLE)
                self._conn.execute(
                    "INSERT INTO plan_items (id, task_id, phase_id, date, seq, content, "
                    "est_minutes, status, priority, done_at) "
                    "SELECT id, task_id, phase_id, date, seq, content, est_minutes, status, "
                    "COALESCE(priority, 0), done_at FROM plan_items_old")
                self._conn.execute("DROP TABLE plan_items_old")
        # 迁移完成后建 priority 相关索引（旧库缺列时无法先建）
        with self._lock, self._conn:
            self._conn.execute(_CREATE_INDEX_STATUS)

    # ── 任务 CRUD ─────────────────────────────────────────────

    def create_task(self, title: str, description: str = "", due_date: str | None = None,
                    priority: str = "normal") -> int:
        if priority not in ("low", "normal", "high"):
            priority = "normal"
        return int(self._exec(
            "INSERT INTO tasks (title, description, due_date, priority) VALUES (?, ?, ?, ?)",
            (title.strip(), description.strip(), due_date, priority), mode="write"))

    def get_task(self, task_id: int) -> dict | None:
        row = self._exec("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        task = dict(row)
        task["phases"] = self.get_phases(task_id)
        task["plan_items"] = self.get_plan(task_id=task_id)
        return task

    def list_tasks(self, status: str | None = None) -> list[dict]:
        if status and status not in _TASK_STATUSES:
            status = None
        if status:
            rows = self._exec(
                "SELECT * FROM tasks WHERE status = ? ORDER BY id DESC", (status,), mode="all")
        else:
            rows = self._exec("SELECT * FROM tasks ORDER BY id DESC", mode="all")
        out = []
        for r in rows:
            t = dict(r)
            plan = self.get_plan(task_id=t["id"])
            t["plan_total"] = len(plan)
            t["plan_done"] = sum(1 for p in plan if p["status"] == "done")
            t["phase_count"] = len(self.get_phases(t["id"]))
            out.append(t)
        return out

    def update_task_status(self, task_id: int, status: str, note: str = "") -> bool:
        if status not in _TASK_STATUSES:
            return False
        changed = self._exec(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id), mode="write") > 0
        if changed and note:
            self.add_review(task_id=task_id, summary=note)
        return changed

    def update_task(self, task_id: int, **fields) -> bool:
        """按白名单更新任务字段（title/description/due_date/priority）。"""
        allowed = {"title", "description", "due_date", "priority"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        return self._exec(
            f"UPDATE tasks SET {sets} WHERE id = ?", (*updates.values(), task_id), mode="write") > 0

    def delete_task(self, task_id: int) -> bool:
        return self._exec("DELETE FROM tasks WHERE id = ?", (task_id,), mode="write") > 0

    # ── 阶段 ──────────────────────────────────────────────────

    def add_phase(self, task_id: int, seq: int, title: str, description: str = "",
                  days: int = 1) -> int:
        return int(self._exec(
            "INSERT INTO phases (task_id, seq, title, description, days) VALUES (?, ?, ?, ?, ?)",
            (task_id, seq, title.strip(), description.strip(), max(1, int(days))), mode="write"))

    def get_phases(self, task_id: int) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM phases WHERE task_id = ? ORDER BY seq", (task_id,), mode="all")
        return [dict(r) for r in rows]

    def set_phase_status(self, phase_id: int, status: str) -> bool:
        if status not in ("pending", "active", "done"):
            return False
        return self._exec(
            "UPDATE phases SET status = ? WHERE id = ?", (status, phase_id), mode="write") > 0

    # ── 待办条目（动态队列：不按固定日期排期）────────────────

    def add_plan_item(self, task_id: int, phase_id: int | None, date_: str | None, seq: int,
                      content: str, est_minutes: int = 0, priority: int = 0) -> int:
        return int(self._exec(
            "INSERT INTO plan_items (task_id, phase_id, date, seq, content, est_minutes, priority) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, phase_id, date_, seq, content.strip(), max(0, int(est_minutes)), int(priority)),
            mode="write"))

    def get_plan(self, date_: str | None = None, task_id: int | None = None,
                 status: str | None = None) -> list[dict]:
        sql = ("SELECT p.*, t.title AS task_title, t.due_date AS task_due, "
               "t.status AS task_status, ph.title AS phase_title "
               "FROM plan_items p "
               "LEFT JOIN tasks t ON t.id = p.task_id "
               "LEFT JOIN phases ph ON ph.id = p.phase_id "
               "WHERE 1=1")
        params: list[Any] = []
        if date_:
            sql += " AND p.date = ?"
            params.append(date_)
        if task_id is not None:
            sql += " AND p.task_id = ?"
            params.append(task_id)
        if status:
            if status not in _PLAN_STATUSES:
                status = None
            else:
                sql += " AND p.status = ?"
                params.append(status)
        sql += " ORDER BY p.priority DESC, p.date IS NULL, p.date, p.seq"
        rows = self._exec(sql, tuple(params), mode="all")
        return [dict(r) for r in rows]

    def list_pending(self) -> list[dict]:
        """动态待办队列：全部未完成条目，按 优先级权重 → 任务截止日期近 → 创建序 排序。"""
        rows = self._exec(
            "SELECT p.*, t.title AS task_title, t.due_date AS task_due, "
            "t.status AS task_status, t.priority AS task_priority, ph.title AS phase_title "
            "FROM plan_items p "
            "LEFT JOIN tasks t ON t.id = p.task_id "
            "LEFT JOIN phases ph ON ph.id = p.phase_id "
            "WHERE p.status != 'done' AND p.status != 'skipped' "
            "AND t.status NOT IN ('done', 'abandoned') "
            "ORDER BY p.priority DESC, "
            "CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END, t.due_date, p.id",
            mode="all")
        return [dict(r) for r in rows]

    def set_item_priority(self, plan_id: int, priority: int) -> bool:
        """动态调整待办条目优先级权重（越大越优先）。"""
        return self._exec(
            "UPDATE plan_items SET priority = ? WHERE id = ?", (int(priority), plan_id),
            mode="write") > 0

    def bump_item_priority(self, plan_id: int) -> bool:
        """把某条待办提到队列最前（priority = 当前最大值 + 1）。"""
        row = self._exec("SELECT MAX(priority) m FROM plan_items")
        top = (row["m"] if row and row["m"] is not None else 0) + 1
        return self.set_item_priority(plan_id, top)

    def get_today_plan(self, today: str | None = None) -> list[dict]:
        return self.get_plan(date_=today or date.today().isoformat())

    def set_plan_status(self, plan_id: int, status: str) -> bool:
        """勾选待办条目（done/skipped/todo）。返回是否生效。"""
        if status not in _PLAN_STATUSES:
            return False
        done_at = None
        if status == "done":
            from datetime import datetime, timezone, timedelta
            done_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        return self._exec(
            "UPDATE plan_items SET status = ?, done_at = ? WHERE id = ?",
            (status, done_at, plan_id), mode="write") > 0

    def list_pending_before(self, before_date: str) -> list[dict]:
        """到期未完成（有条目日期且逾期）——动态队列下仅作参考。"""
        rows = self._exec(
            "SELECT p.*, t.title AS task_title FROM plan_items p "
            "LEFT JOIN tasks t ON t.id = p.task_id "
            "WHERE p.date IS NOT NULL AND p.date < ? AND p.status != 'done' "
            "AND p.status != 'skipped' ORDER BY p.date",
            (before_date,), mode="all")
        return [dict(r) for r in rows]

    def list_overdue_tasks(self, today: str | None = None) -> list[dict]:
        """逾期任务：due_date < today 且任务未完成（动态提醒用）。"""
        today = today or date.today().isoformat()
        rows = self._exec(
            "SELECT * FROM tasks WHERE due_date IS NOT NULL AND due_date < ? "
            "AND status NOT IN ('done', 'abandoned') ORDER BY due_date",
            (today,), mode="all")
        return [dict(r) for r in rows]

    # ── 回访记录 ──────────────────────────────────────────────

    def add_review(self, task_id: int | None = None, plan_item_id: int | None = None,
                   summary: str = "") -> int:
        return int(self._exec(
            "INSERT INTO reviews (task_id, plan_item_id, summary) VALUES (?, ?, ?)",
            (task_id, plan_item_id, summary.strip()), mode="write"))

    def list_reviews(self, task_id: int | None = None, limit: int = 20) -> list[dict]:
        if task_id is None:
            rows = self._exec(
                "SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,), mode="all")
        else:
            rows = self._exec(
                "SELECT * FROM reviews WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, limit), mode="all")
        return [dict(r) for r in rows]

    # ── 汇总 ──────────────────────────────────────────────────

    def summary(self, today: str | None = None) -> dict:
        """一次性汇总（/state 与调度注入用）：动态待办队列为主。"""
        today = today or date.today().isoformat()
        counts = {}
        for s in _TASK_STATUSES:
            row = self._exec("SELECT COUNT(*) c FROM tasks WHERE status = ?", (s,))
            counts[s] = row["c"]
        pending = self.list_pending()
        return {
            "today": today,
            "tasks": counts,
            "pending_total": len(pending),
            "pending_done": sum(1 for p in pending if p["status"] == "done"),
            "queue": pending,                     # 动态待办队列（已排序）
            "overdue_tasks": self.list_overdue_tasks(today),
        }

    @staticmethod
    def add_days(date_: str, days: int) -> str:
        """YYYY-MM-DD + n 天（拆解排期用）。"""
        d = date.fromisoformat(date_)
        return (d + timedelta(days=days)).isoformat()

