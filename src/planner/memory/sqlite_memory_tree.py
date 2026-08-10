"""SQLite 分层摘要记忆树（独立移植自 xiaob.memory.sqlite_memory_tree）。

与 xiaob 的差异：
- 删除旧 JSON 数据迁移（_maybe_import_legacy）与 buffer 压缩编排（compress_buffer/
  compact_buffer）——压缩请求改由 middleware 层（SummarizationMiddleware）用
  BaseChatModel 发起，树只负责存储与查询；
- 保留 nodes/buffer_state 表结构与全部查询/写入接口语义。

压缩策略常量：LEAF_SIZE=20、BRANCHING_FACTOR=4（每层 ≥8 个节点时取前 4 个合并）。
"""

from __future__ import annotations

import json
import threading
import sqlite3
import time
from pathlib import Path
from typing import Any

LEAF_SIZE = 20
BRANCHING_FACTOR = 4
LEVEL_COMPACT_THRESHOLD = 8

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS nodes (
    character_id TEXT NOT NULL,
    id TEXT NOT NULL,
    level INTEGER NOT NULL,
    summary TEXT NOT NULL,
    parent_id TEXT,
    round_start INTEGER,
    round_end INTEGER,
    time_start TEXT,
    time_end TEXT,
    source_ref TEXT,
    details TEXT,
    profile TEXT,
    meta TEXT,
    is_active INTEGER DEFAULT 1,
    created_at INTEGER DEFAULT (unixepoch()),
    PRIMARY KEY (character_id, id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_character_level_active ON nodes(character_id, level, is_active);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS buffer_state (
    character_id TEXT PRIMARY KEY,
    recent_buffer TEXT,
    msg_counter INTEGER,
    round INTEGER,
    last_activity_at REAL,
    reminded_overdue TEXT,
    compressed_total INTEGER,
    next_heartbeat_at REAL,
    heartbeat_minutes REAL,
    heartbeat_note TEXT,
    last_player_message_at REAL
);
"""


class SQLiteMemoryTree:
    """基于 SQLite 的分层摘要记忆树（单角色，character_id 区分）。"""

    def __init__(self, character_id: str, db_path: Path) -> None:
        self._character_id = character_id
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._lock = threading.RLock()   # 单连接多线程（主线程 + 异步压缩线程）串行化
        self._conn.row_factory = sqlite3.Row
        # WAL：读写不互斥；busy_timeout：锁竞争时等待而非立即报错
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()
        self._level_counters = self._init_level_counters()

    def close(self) -> None:
        """关闭数据库连接（进程退出/会话销毁时调用）。"""
        with self._lock:
            self._conn.close()

    # ── 数据库初始化 ───────────────────────────────────────────

    def _init_db(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.executescript(_CREATE_TABLES)
        # compat: add is_active column if missing
        cur = self._execute_with_retry(
            "SELECT name FROM pragma_table_info('nodes') WHERE name = 'is_active'"
        )
        if not cur.fetchone():
            with self._lock:
                with self._conn:
                    self._execute_with_retry("ALTER TABLE nodes ADD COLUMN is_active INTEGER DEFAULT 1")
        # compat: buffer_state 加列（重启回归问候 + 逾期去重持久化，老库平滑升级）
        for col in ("last_activity_at", "reminded_overdue", "compressed_total",
                    "next_heartbeat_at", "heartbeat_minutes", "heartbeat_note",
                    "last_player_message_at"):
            cur = self._execute_with_retry(
                f"SELECT name FROM pragma_table_info('buffer_state') WHERE name = '{col}'"
            )
            if not cur.fetchone():
                with self._lock:
                    with self._conn:
                        self._execute_with_retry(f"ALTER TABLE buffer_state ADD COLUMN {col}")
        # compat: add profile column if missing（画像字段）
        cur = self._execute_with_retry(
            "SELECT name FROM pragma_table_info('nodes') WHERE name = 'profile'"
        )
        if not cur.fetchone():
            with self._lock:
                with self._conn:
                    self._execute_with_retry("ALTER TABLE nodes ADD COLUMN profile TEXT")
        # compat: add meta column if missing（schema_version + 未来扩展字段）
        cur = self._execute_with_retry(
            "SELECT name FROM pragma_table_info('nodes') WHERE name = 'meta'"
        )
        if not cur.fetchone():
            with self._lock:
                with self._conn:
                    self._execute_with_retry("ALTER TABLE nodes ADD COLUMN meta TEXT")
        # compat: 节点覆盖时间范围（压缩时取原始消息起止时间）
        for col in ("time_start", "time_end"):
            cur = self._execute_with_retry(
                f"SELECT name FROM pragma_table_info('nodes') WHERE name = '{col}'"
            )
            if not cur.fetchone():
                with self._lock:
                    with self._conn:
                        self._execute_with_retry(f"ALTER TABLE nodes ADD COLUMN {col} TEXT")

    def _execute_with_retry(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """带简单重试的 SQL 执行（锁内串行，处理多线程/多连接并发）。"""
        with self._lock:
            last_exc = None
            for _ in range(3):
                try:
                    return self._conn.execute(sql, params)
                except sqlite3.OperationalError as exc:
                    last_exc = exc
                    if "locked" in str(exc).lower():
                        time.sleep(0.05)
                    continue
                raise
        raise last_exc

    def _init_level_counters(self) -> dict[int, int]:
        """从数据库恢复各层级最大编号。"""
        counters: dict[int, int] = {}
        cur = self._execute_with_retry(
            "SELECT level, MAX(CAST(SUBSTR(id, INSTR(id, '_') + 1) AS INTEGER)) as max_num "
            "FROM nodes WHERE character_id = ? GROUP BY level",
            (self._character_id,),
        )
        for row in cur.fetchall():
            counters[row["level"]] = row["max_num"]
        return counters

    def _next_id(self, level: int) -> str:
        self._level_counters[level] = self._level_counters.get(level, 0) + 1
        return f"node{level}_{self._level_counters[level]:03d}"

    # ── 公开查询接口 ──────────────────────────────────────────

    def get_root_id(self) -> str | None:
        """返回根节点 id（level 最高的节点，多个取最早创建的）。"""
        cur = self._execute_with_retry(
            "SELECT id FROM nodes WHERE character_id = ? ORDER BY level DESC, id LIMIT 1",
            (self._character_id,),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def get_effective_time_range(self, node_id: str) -> tuple[str, str] | None:
        """节点有效时间范围 (start, end)：自身 time_start/time_end 优先；
        为 NULL（时间范围功能上线前压缩的历史节点）时递归聚合子节点 min/max。"""
        cur = self._execute_with_retry(
            "SELECT time_start, time_end FROM nodes WHERE id = ? AND character_id = ?",
            (node_id, self._character_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        own = (row["time_start"], row["time_end"])
        if own[0] and own[1]:
            return own
        cur = self._execute_with_retry(
            "SELECT id FROM nodes WHERE parent_id = ? AND character_id = ?",
            (node_id, self._character_id),
        )
        children = [r["id"] for r in cur.fetchall()]
        start, end = own[0], own[1]
        for cid in children:
            rng = self.get_effective_time_range(cid)
            if not rng:
                continue
            if not start or rng[0] < start:
                start = rng[0]
            if not end or rng[1] > end:
                end = rng[1]
        return (start, end) if (start and end) else None

    def get_node_children_info(self, node_id: str) -> dict | None:
        """获取节点的子节点信息或叶子详情（含画像、后续说明与 meta）。"""
        cur = self._execute_with_retry(
            "SELECT level, details, profile, meta, time_start, time_end FROM nodes WHERE id = ? AND character_id = ?",
            (node_id, self._character_id),
        )
        row = cur.fetchone()
        if row is None:
            return None

        profile = None
        if row["profile"]:
            try:
                profile = json.loads(row["profile"])
            except json.JSONDecodeError:
                profile = None

        meta = None
        if row["meta"]:
            try:
                meta = json.loads(row["meta"])
            except json.JSONDecodeError:
                meta = None

        if row["level"] == 0:
            details = []
            future_notes = None
            if row["details"]:
                try:
                    details = json.loads(row["details"])
                except json.JSONDecodeError:
                    details = []
            # 新格式：{"messages": [...], "future_notes": [...]}；旧格式：纯列表
            if isinstance(details, dict):
                future_notes = details.get("future_notes")
                details = details.get("messages", [])
            return {"details": details, "profile": profile, "future_notes": future_notes,
                    "meta": meta, "time_start": row["time_start"], "time_end": row["time_end"]}

        cur = self._execute_with_retry(
            "SELECT id, summary, profile FROM nodes WHERE parent_id = ? AND character_id = ? ORDER BY round_start",
            (node_id, self._character_id),
        )
        children = []
        for r in cur.fetchall():
            cp = None
            if r["profile"]:
                try:
                    cp = json.loads(r["profile"])
                except json.JSONDecodeError:
                    cp = None
            children.append({"node_id": r["id"], "summary": r["summary"], "profile": cp})
        return {"children": children, "profile": profile, "meta": meta,
                "time_start": row["time_start"], "time_end": row["time_end"]}

    # ── 写入接口 ──────────────────────────────────────────────

    def add_leaf(
        self,
        summary: str,
        round_range: tuple[int, int],
        source_ref: str,
        details: list[dict[str, str]] | None = None,
        profile: dict | None = None,
        future_notes: list[str] | None = None,
        meta: dict | None = None,
        time_range: tuple[str, str] | None = None,
    ) -> str:
        """新增一个叶子节点，返回 node_id。

        profile: 用户画像 JSON（preferences/personality/habits/goals）；
        future_notes: 后续说明（结合未来消息的澄清/修正）——存进 details 结构；
        meta: schema_version 与未来扩展字段（JSON）；
        time_range: 覆盖的原始对话起止时间（(start, end) ISO 秒级字符串）。
        """
        node_id = self._next_id(0)
        rr = list(round_range)
        tr = list(time_range) if time_range else [None, None]
        details_payload = details
        if future_notes:
            details_payload = {"messages": details or [], "future_notes": future_notes}
        with self._lock:
            with self._conn:
                self._execute_with_retry(
                    "INSERT INTO nodes (id, character_id, level, summary, parent_id, round_start, round_end, time_start, time_end, source_ref, details, profile, meta, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        self._character_id,
                        0,
                        summary,
                        None,
                        rr[0],
                        rr[1],
                        tr[0],
                        tr[1],
                        source_ref,
                        json.dumps(details_payload, ensure_ascii=False) if details_payload else None,
                        json.dumps(profile, ensure_ascii=False) if profile else None,
                        json.dumps(meta, ensure_ascii=False) if meta else None,
                        1,
                    ),
                )
        return node_id

    def compact(self, child_ids: list[str], summary: str,
                profile: dict | None = None,
                future_notes: list[str] | None = None,
                meta: dict | None = None) -> str:
        """将多个子节点压缩成一个父节点，返回父节点 node_id。

        父节点时间范围 = 子节点 time_start 最小 / time_end 最大（ISO 字符串 min/max）。
        """
        if not child_ids:
            raise ValueError("compact: child_ids 不能为空")

        children_level = None
        round_start = None
        round_end = None
        time_start = None
        time_end = None
        for cid in child_ids:
            cur = self._execute_with_retry(
                "SELECT level, round_start, round_end, time_start, time_end "
                "FROM nodes WHERE id = ? AND character_id = ?",
                (cid, self._character_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"compact: 子节点 {cid} 不存在")
            if children_level is None:
                children_level = row["level"]
            rs, re = row["round_start"], row["round_end"]
            if round_start is None or (rs is not None and rs < round_start):
                round_start = rs
            if round_end is None or (re is not None and re > round_end):
                round_end = re
            ts, te = row["time_start"], row["time_end"]
            if ts and (time_start is None or ts < time_start):
                time_start = ts
            if te and (time_end is None or te > time_end):
                time_end = te

        parent_level = (children_level or 0) + 1
        parent_id = self._next_id(parent_level)

        with self._lock:
            with self._conn:
                self._execute_with_retry(
                    "INSERT INTO nodes (id, character_id, level, summary, parent_id, round_start, round_end, time_start, time_end, profile, meta, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (parent_id, self._character_id, parent_level, summary, None, round_start, round_end,
                     time_start, time_end,
                     json.dumps(profile, ensure_ascii=False) if profile else None,
                     json.dumps(meta, ensure_ascii=False) if meta else None, 1),
                )
                for cid in child_ids:
                    self._execute_with_retry(
                        "UPDATE nodes SET parent_id = ?, is_active = 0 WHERE id = ? AND character_id = ?",
                        (parent_id, cid, self._character_id),
                    )
        return parent_id

    # ── 节点数查询 ───────────────────────────────────────────

    def get_nodes_at_level(self, level: int) -> list[dict[str, Any]]:
        """获取指定层级的当前活跃节点（is_active = 1）。"""
        cur = self._execute_with_retry(
            "SELECT id, level, summary, parent_id, round_start, round_end, time_start, time_end, source_ref, details, profile, meta, is_active "
            "FROM nodes WHERE character_id = ? AND level = ? AND is_active = 1 ORDER BY round_start",
            (self._character_id, level),
        )
        result = []
        for r in cur.fetchall():
            node = dict(r)
            node["round_range"] = [node.pop("round_start"), node.pop("round_end")]
            node.pop("is_active", None)
            if node["details"]:
                try:
                    node["details"] = json.loads(node["details"])
                except json.JSONDecodeError:
                    node["details"] = []
            else:
                node["details"] = []
            if node.get("profile"):
                try:
                    node["profile"] = json.loads(node["profile"])
                except json.JSONDecodeError:
                    node["profile"] = None
            if node.get("meta"):
                try:
                    node["meta"] = json.loads(node["meta"])
                except json.JSONDecodeError:
                    node["meta"] = None
            node["children"] = []
            result.append(node)
        return result

    def get_level_count(self, level: int) -> int:
        """获取指定层级的当前活跃节点数量。"""
        cur = self._execute_with_retry(
            "SELECT COUNT(*) as cnt FROM nodes WHERE character_id = ? AND level = ? AND is_active = 1",
            (self._character_id, level),
        )
        return cur.fetchone()["cnt"]

    # ── buffer 状态（重启恢复）────────────────────────────────

    def save_buffer_state(self, recent_buffer: list[dict], msg_counter: int, round: int,
                          last_activity_at: float | None = None,
                          reminded_overdue: list | None = None,
                          compressed_total: int = 0,
                          next_heartbeat_at: float | None = None,
                          heartbeat_minutes: float | None = None,
                          heartbeat_note: str | None = None,
                          last_player_message_at: float | None = None) -> None:
        """保存 recent_buffer 状态到 SQLite（重启恢复）。"""
        with self._lock:
            with self._conn:
                self._execute_with_retry(
                    "INSERT OR REPLACE INTO buffer_state "
                    "(character_id, recent_buffer, msg_counter, round, last_activity_at, reminded_overdue, "
                    "compressed_total, next_heartbeat_at, heartbeat_minutes, heartbeat_note, "
                    "last_player_message_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._character_id,
                        json.dumps(recent_buffer, ensure_ascii=False),
                        msg_counter,
                        round,
                        last_activity_at,
                        json.dumps(reminded_overdue or [], ensure_ascii=False),
                        int(compressed_total or 0),
                        next_heartbeat_at,
                        heartbeat_minutes,
                        heartbeat_note,
                        last_player_message_at,
                    ),
                )

    def load_buffer_state(self) -> dict | None:
        """从 SQLite 读取 recent_buffer 状态。"""
        cur = self._execute_with_retry(
            "SELECT recent_buffer, msg_counter, round, last_activity_at, reminded_overdue, "
            "compressed_total, next_heartbeat_at, heartbeat_minutes, heartbeat_note, "
            "last_player_message_at "
            "FROM buffer_state WHERE character_id = ?",
            (self._character_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            recent_buffer = json.loads(row["recent_buffer"]) if row["recent_buffer"] else []
        except json.JSONDecodeError:
            recent_buffer = []
        try:
            reminded = json.loads(row["reminded_overdue"]) if row["reminded_overdue"] else []
        except (json.JSONDecodeError, TypeError):
            reminded = []
        return {
            "recent_buffer": recent_buffer,
            "_msg_counter": row["msg_counter"],
            "round": row["round"],
            "last_activity_at": row["last_activity_at"] or 0.0,
            "reminded_overdue": reminded,
            "compressed_total": int(row["compressed_total"] or 0),
            "next_heartbeat_at": row["next_heartbeat_at"] or 0.0,
            "heartbeat_minutes": row["heartbeat_minutes"] or 0.0,
            "heartbeat_note": row["heartbeat_note"] or "",
            "last_player_message_at": row["last_player_message_at"] or 0.0,
        }

    def clear_character_data(self) -> None:
        """清除本角色的所有记忆树节点和 buffer_state。"""
        with self._lock:
            with self._conn:
                self._execute_with_retry(
                    "DELETE FROM nodes WHERE character_id = ?",
                    (self._character_id,),
                )
                self._execute_with_retry(
                    "DELETE FROM buffer_state WHERE character_id = ?",
                    (self._character_id,),
                )
        self._level_counters.clear()
