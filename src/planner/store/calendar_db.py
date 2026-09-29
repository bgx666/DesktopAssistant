"""SQLite 日历库：日程 / 提醒（calendar_items）。

数据模型：
- calendar_items  按时间排的日程/提醒（用户可见可编辑）；可带时刻，可按规则重复

所有日期均为 YYYY-MM-DD 字符串（UTC+8 本地日）；time 为 HH:MM 或 NULL（全天）。

并发：连接可被 handler / worker / 调度线程共用（check_same_thread=False），
所有语句执行与 close() 都经由同一把 RLock 串行化——避免「execute 成功、
另一线程 close、fetchone 返回 None」的竞态（实测修复 2026-08-05）。

历史沿革（2026-09-12）：本模块原为 TasksDb（任务 / 阶段 / 日计划 / 回访记录），
那套「长期目标 + 推进队列」已整体移除——规划统一走日历，不再有两套待办概念。
遗留的四张表（tasks/phases/plan_items/reviews）由 _init_db 一次性 DROP，
旧数据只保留在迁移前的库备份里。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS calendar_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                       -- YYYY-MM-DD（UTC+8 本地日）
    time TEXT,                                -- HH:MM；NULL = 全天
    kind TEXT NOT NULL DEFAULT 'event',       -- event 日程 / task 当天任务
    content TEXT NOT NULL,
    detail TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',      -- todo / done / cancelled
    reminded_at TEXT,                         -- 到点提醒过的时刻（防重复提醒）
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    done_at TEXT,
    series_id TEXT,                           -- 重复系列标识（NULL = 一次性）
    repeat TEXT,                              -- 规则 daily/weekly/weekdays（同系列各行相同）
    repeat_until TEXT                         -- 该系列已生成到的最后日期（滚动续期用）
);

CREATE INDEX IF NOT EXISTS idx_calendar_date ON calendar_items(date, time);
"""

# 已移除的“任务/阶段/待办/回访”模块遗留表（先子后父，避免外键残留）
_LEGACY_TABLES = ("plan_items", "phases", "reviews", "tasks")


class CalendarDb:
    """日历库（单连接，SQLite WAL，RLock 串行化全部访问）。"""


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

        mode: one → 返回单行；all → 返回全部行；write → 自动 commit，
        **INSERT 返回新 id（lastrowid），其余（UPDATE/DELETE）返回受影响行数**。

        ⚠️ UPDATE/DELETE 的 `cur.lastrowid` 会残留**上一次 INSERT** 的 rowid
        （sqlite3 实测：改 0 行、改 1 行都返回同一个残留值），拿它当"改动行数"
        会让 `> 0` 判断恒为真 → 改 0 行也报成功。必须按语句类型分别取。
        """
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError(f"CalendarDb 已关闭: {self._db_path}")
            cur = self._conn.execute(sql, params)
            if mode == "one":
                return cur.fetchone()
            if mode == "all":
                return cur.fetchall()
            self._conn.commit()
            if sql.lstrip()[:6].upper() == "INSERT":
                return cur.lastrowid if cur.lastrowid else cur.rowcount
            return cur.rowcount

    def _init_db(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_CREATE_TABLES)
        # 迁移：重复规则列（旧库的 calendar_items 没有 → 逐个补；可空 TEXT，无需重建表）
        cols = {r["name"] for r in self._exec(
            "SELECT name FROM pragma_table_info('calendar_items')", mode="all")}
        for col in ("series_id", "repeat", "repeat_until"):
            if col not in cols:
                with self._lock, self._conn:
                    self._exec(f"ALTER TABLE calendar_items ADD COLUMN {col} TEXT")
        # 迁移：去掉指向已移除 tasks 表的 task_id 列（SQLite 3.35+ 支持 DROP COLUMN）
        if "task_id" in cols:
            with self._lock, self._conn:
                self._conn.execute("ALTER TABLE calendar_items DROP COLUMN task_id")
        # 一次性清理：DROP 已移除模块的遗留表（master 明确要求不留历史残留）
        for table in _LEGACY_TABLES:
            with self._lock, self._conn:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")

    # ── 日历（calendar_items）──────────────────────────────────
    # 这是**唯一**的待办/日程概念：按时间排，用户可见可编辑，模型也能读写。
    # 日历项带可选时刻；到点的提醒由调度线程扫描触发（reminded_at 防重复）；
    # 带规则的项由 add_calendar_series 生成实体行，滚动续期兜住"无限重复"。
    # 表与连接的所有权都在本类里——沿用同一个连接与 RLock，不另开连接。

    CALENDAR_KINDS = ("event", "task")       # event=日程（占时段）/ task=当天任务
    CALENDAR_STATUSES = ("todo", "done", "cancelled")

    def add_calendar_item(self, date_: str, content: str, time_: str | None = None,
                          kind: str = "event", detail: str = "") -> int:
        """新增日历项。time_ 为 "HH:MM"，None = 全天。返回新 id。"""
        kind = kind if kind in self.CALENDAR_KINDS else "event"
        return int(self._exec(
            "INSERT INTO calendar_items (date, time, kind, content, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (date_, time_ or None, kind, content.strip(), detail or ""),
            mode="write"))

    def get_calendar_item(self, item_id: int) -> dict | None:
        row = self._exec("SELECT * FROM calendar_items WHERE id = ?", (item_id,))
        return dict(row) if row is not None else None

    def list_calendar_range(self, start: str, end: str,
                            include_cancelled: bool = False) -> list[dict]:
        """按时间范围查日历项（闭区间，date 为 YYYY-MM-DD）。

        排序：先按日期，再按时刻（全天项排当天最前，视为最早）。
        """
        sql = ("SELECT * FROM calendar_items WHERE date >= ? AND date <= ? ")
        if not include_cancelled:
            sql += "AND status != 'cancelled' "
        sql += "ORDER BY date, COALESCE(time, '00:00'), id"
        return [dict(r) for r in self._exec(sql, (start, end), mode="all")]

    def list_calendar_day(self, date_: str) -> list[dict]:
        return self.list_calendar_range(date_, date_)

    # ── 重复系列（生成实体行，而非查询时虚拟展开）──────────────────
    # 为什么这么选：到点提醒靠 `list_calendar_due` + `reminded_at` 防重复，这套是
    # **按真实的行**工作的。虚拟实例要支持"这一次提醒过了/做过了"，终究还得落成行——
    # 所以直接生成行反而更简单，且勾完成 / 取消某一次 / 改期 全部现有代码直接复用。
    # 代价：表达不了"无限重复"，只能生成到 horizon；由滚动续期兜住（不会默默停掉）。

    REPEAT_RULES = ("daily", "weekly", "weekdays")
    REPEAT_HORIZON_DAYS = 365        # 每次生成/续期的跨度
    REPEAT_EXTEND_WHEN_LEFT = 60     # 系列剩余不足这么多天时续期

    @staticmethod
    def _series_dates(rule: str, start: str, until: str, base_date: str) -> list[str]:
        """按规则算 [start, until] 内的日期。

        base_date 决定 weekly 是"星期几"——续期时它必须仍是最初的起始日，
        否则用续期起点当基准会漂移到别的星期几。
        """
        d0, d1 = date.fromisoformat(start), date.fromisoformat(until)
        if d1 < d0:
            return []
        base_wd = date.fromisoformat(base_date).weekday()   # 0=周一
        out, cur = [], d0
        while cur <= d1:
            if rule == "daily":
                hit = True
            elif rule == "weekly":
                hit = cur.weekday() == base_wd
            elif rule == "weekdays":
                hit = cur.weekday() < 5
            else:
                hit = False
            if hit:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out

    def add_calendar_series(self, date_: str, content: str, rule: str,
                            time_: str | None = None, kind: str = "event",
                            detail: str = "",
                            horizon_days: int | None = None) -> tuple[str, int]:
        """按重复规则生成一串日历项，返回 (series_id, 生成条数)。

        rule：daily（每天）/ weekly（每周与 date_ 同一个星期几）/ weekdays（工作日）。
        生成到 date_ + horizon（默认 1 年），之后由滚动续期自动补。
        """
        if rule not in self.REPEAT_RULES:
            raise ValueError(f"不支持的重复规则: {rule!r}")
        horizon = int(horizon_days or self.REPEAT_HORIZON_DAYS)
        until = (date.fromisoformat(date_) + timedelta(days=horizon)).isoformat()
        dates = self._series_dates(rule, date_, until, date_)
        if not dates:
            return "", 0
        sid = uuid.uuid4().hex[:12]
        kind = kind if kind in self.CALENDAR_KINDS else "event"
        text = content.strip()
        with self._lock, self._conn:
            for d in dates:
                self._conn.execute(
                    "INSERT INTO calendar_items "
                    "(date, time, kind, content, detail, series_id, repeat, repeat_until) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (d, time_ or None, kind, text, detail or "", sid, rule, until))
            self._conn.commit()
        return sid, len(dates)

    def list_calendar_series(self) -> list[dict]:
        """所有重复系列（每系列一行）：已生成到哪天、还剩多少未完成。"""
        rows = self._exec(
            "SELECT series_id, MIN(date) AS first_date, MAX(date) AS last_date, "
            "MAX(repeat) AS repeat, MAX(repeat_until) AS repeat_until, "
            "MAX(content) AS content, MAX(time) AS time, MAX(kind) AS kind, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'todo' THEN 1 ELSE 0 END) AS todo_n "
            "FROM calendar_items WHERE series_id IS NOT NULL "
            "GROUP BY series_id ORDER BY MIN(date)", mode="all")
        return [dict(r) for r in rows]

    def extend_calendar_series(self, series_id: str,
                               horizon_days: int | None = None,
                               today: str | None = None) -> int:
        """把系列续到 今天 + horizon（返回新增条数；已够长则 0）。

        today 可显式传入——续期是"未来才发生"的行为，不给注入点就没法测。
        """
        row = self._exec(
            "SELECT MIN(date) AS first_date, MAX(repeat_until) AS until, "
            "MAX(repeat) AS repeat, MAX(content) AS content, MAX(time) AS time, "
            "MAX(kind) AS kind, MAX(detail) AS detail "
            "FROM calendar_items WHERE series_id = ? GROUP BY series_id", (series_id,))
        if row is None or not row["repeat"]:
            return 0
        horizon = int(horizon_days or self.REPEAT_HORIZON_DAYS)
        base_day = date.fromisoformat(today) if today else date.today()
        target = (base_day + timedelta(days=horizon)).isoformat()
        last = row["until"] or row["first_date"]
        if target <= last:
            return 0
        start = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        # base 仍用最初起始日，保证 weekly 不漂移
        dates = self._series_dates(row["repeat"], start, target, row["first_date"])
        if not dates:
            return 0
        with self._lock, self._conn:
            for d in dates:
                self._conn.execute(
                    "INSERT INTO calendar_items "
                    "(date, time, kind, content, detail, series_id, repeat, repeat_until) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (d, row["time"] or None,
                     row["kind"] if row["kind"] in self.CALENDAR_KINDS else "event",
                     row["content"] or "", row["detail"] or "",
                     series_id, row["repeat"], target))
            self._conn.commit()
        return len(dates)

    def list_calendar_series_need_extend(self, today: str | None = None) -> list[str]:
        """需要续期的系列 id（已生成到的日期就在 REPEAT_EXTEND_WHEN_LEFT 天以内）。"""
        today = today or date.today().isoformat()
        limit = (date.fromisoformat(today)
                 + timedelta(days=self.REPEAT_EXTEND_WHEN_LEFT)).isoformat()
        rows = self._exec(
            "SELECT series_id, MAX(COALESCE(repeat_until, date)) AS last "
            "FROM calendar_items WHERE series_id IS NOT NULL AND status != 'cancelled' "
            "GROUP BY series_id HAVING last <= ?", (limit,), mode="all")
        return [r["series_id"] for r in rows]

    def update_calendar_series(self, series_id: str, from_date: str | None = None,
                               **fields) -> int:
        """批量改系列里"from_date 起、未完成"的行（改内容/时刻/类型）。返回改动行数。

        不动已完成的（那是历史），也不动 cancelled 的。
        """
        allowed = {"time", "kind", "content", "detail"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return 0
        sql = (f"UPDATE calendar_items SET {', '.join(f'{k} = ?' for k in sets)} "
               "WHERE series_id = ? AND status = 'todo'")
        params: list = list(sets.values()) + [series_id]
        if from_date:
            sql += " AND date >= ?"
            params.append(from_date)
        return int(self._exec(sql, tuple(params), mode="write"))

    def cancel_calendar_series(self, series_id: str, from_date: str | None = None) -> int:
        """取消系列里"from_date 起、未完成"的行（软取消，保留痕迹）。返回行数。"""
        sql = ("UPDATE calendar_items SET status = 'cancelled' "
               "WHERE series_id = ? AND status = 'todo'")
        params: list = [series_id]
        if from_date:
            sql += " AND date >= ?"
            params.append(from_date)
        return int(self._exec(sql, tuple(params), mode="write"))

    def update_calendar_item(self, item_id: int, **fields) -> bool:
        """改日历项（白名单字段：date/time/kind/content/detail/status）。"""
        allowed = {"date", "time", "kind", "content", "detail", "status"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        assigns = ", ".join(f"{k} = ?" for k in sets)
        # 状态变化时同步 done_at：标完成写入时间，撤销完成清空（避免残留成"已完成但没时间"）
        if sets.get("status") is None:
            marks = ""
        elif sets["status"] == "done":
            marks = ", done_at = datetime('now', '+8 hours')"
        else:
            marks = ", done_at = NULL"
        n = self._exec(f"UPDATE calendar_items SET {assigns}{marks} WHERE id = ?",
                       (*sets.values(), item_id), mode="write")
        return bool(n)

    def set_calendar_status(self, item_id: int, status: str) -> bool:
        if status not in self.CALENDAR_STATUSES:
            return False
        marks = ", done_at = datetime('now', '+8 hours')" if status == "done" else ", done_at = NULL"
        n = self._exec(f"UPDATE calendar_items SET status = ?{marks} WHERE id = ?",
                       (status, item_id), mode="write")
        return bool(n)

    def cancel_calendar_item(self, item_id: int) -> bool:
        """取消（软删：status='cancelled'，保留痕迹）。"""
        return self.set_calendar_status(item_id, "cancelled")

    def list_calendar_due(self, now_: datetime) -> list[dict]:
        """到点且尚未提醒过的待办项（供调度线程扫描）。

        判据：status='todo' 且 (date, time) <= now 且 reminded_at IS NULL。
        全天项（time 为 NULL）不参与到点提醒——由当天的晨间提示覆盖。
        """
        stamp = now_.strftime("%Y-%m-%d %H:%M")
        rows = self._exec(
            "SELECT * FROM calendar_items "
            "WHERE status = 'todo' AND time IS NOT NULL AND reminded_at IS NULL "
            "AND (date || ' ' || time) <= ? ORDER BY date, time",
            (stamp,), mode="all")
        return [dict(r) for r in rows]

    def mark_calendar_reminded(self, item_id: int, at: str) -> None:
        self._exec("UPDATE calendar_items SET reminded_at = ? WHERE id = ?",
                   (at, item_id), mode="write")

    def list_overdue_calendar(self, now_: datetime) -> list[dict]:
        """已过时刻但还没做也没提醒的项（跨重启补提醒用，含 status='todo'）。"""
        stamp = now_.strftime("%Y-%m-%d %H:%M")
        rows = self._exec(
            "SELECT * FROM calendar_items WHERE status = 'todo' "
            "AND (date || COALESCE(' ' || time, ' 23:59')) < ? ORDER BY date, time",
            (stamp,), mode="all")
        return [dict(r) for r in rows]

