"""目录授权表（SQLite）：目前管理「只读」目录，写权限仍只在当前工作区内。

语义：
- 授权按目录、递归生效：授权 D:\\projects 也覆盖其所有子目录；
- 子目录已被授权时再授权父目录 → 不重复插入，由父目录覆盖（列表合并显示）；
- 删除父目录授权不会误删之前显式授权的子目录（子目录会重新出现在列表里）；
- 授权只保存到 `data/permissions.db`，与旧 `data/permissions.json` 的
  「单文件/命令记住并允许」规则互不影响。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from .workspace import is_blocked, is_within

_logger = logging.getLogger("planner.permissions")

_CREATE = """
CREATE TABLE IF NOT EXISTS path_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'read',
    recursive INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_path_grants_key_mode
    ON path_grants(path_key, mode);
"""


class PermissionDb:
    """目录读授权的 SQLite 存储（短连接，方法内部串行）。"""

    def __init__(self, data_root: Path) -> None:
        self.path = Path(data_root) / "permissions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as conn:
                conn.executescript(_CREATE)
        except sqlite3.Error:
            _logger.exception("[permissions] 初始化授权表失败: %s", self.path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def normalize(path: str | Path) -> Path:
        p = Path(str(path or "").strip()).expanduser()
        try:
            return p.resolve()
        except OSError:
            return Path(os.path.abspath(str(p)))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "path": str(row["path"]),
            "mode": str(row["mode"]),
            "recursive": bool(row["recursive"]),
            "source": str(row["source"]),
            "created_at": float(row["created_at"]),
        }

    def add_read(self, path: str | Path, source: str = "user") -> dict:
        """授权一个目录的递归读权限；已被子/父目录覆盖时做合并。"""
        if not str(path or "").strip():
            return {"ok": False, "error": "bad_path", "message": "目录路径为空。"}
        p = self.normalize(path)
        if not p.is_dir():
            return {"ok": False, "error": "not_a_directory",
                    "message": "只能授权已存在的目录。"}
        if is_blocked(p):
            return {"ok": False, "error": "blocked",
                    "message": "该目录在保护名单内，不能授权。"}
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM path_grants WHERE mode = 'read' ORDER BY id"
                ).fetchall()
                for row in rows:
                    if is_within(Path(str(row["path"])), p):
                        return {"ok": True, "merged": True,
                                "grant": self._row(row), "grants": self.list_effective()}
                now = time.time()
                cur = conn.execute(
                    "INSERT INTO path_grants (path, path_key, mode, recursive, source, created_at) "
                    "VALUES (?, ?, 'read', 1, ?, ?)",
                    (str(p), os.path.normcase(str(p)), str(source or "user"), now),
                )
                row = conn.execute("SELECT * FROM path_grants WHERE id = ?",
                                   (cur.lastrowid,)).fetchone()
            return {"ok": True, "merged": False,
                    "grant": self._row(row), "grants": self.list_effective()}
        except sqlite3.Error as exc:
            _logger.exception("[permissions] 添加读授权失败: %s", p)
            return {"ok": False, "error": "db_error", "message": str(exc)}

    def has_read(self, path: str | Path) -> bool:
        p = self.normalize(path)
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT path FROM path_grants WHERE mode = 'read'").fetchall()
            return any(is_within(Path(str(r["path"])), p) for r in rows)
        except sqlite3.Error:
            _logger.exception("[permissions] 查询读授权失败")
            return False

    def list_effective(self) -> list[dict]:
        """返回合并后的顶层授权；被子授权覆盖的目录不再重复显示。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM path_grants WHERE mode = 'read' ORDER BY created_at, id"
                ).fetchall()
        except sqlite3.Error:
            _logger.exception("[permissions] 读取授权列表失败")
            return []
        items = [self._row(r) for r in rows]
        out: list[dict] = []
        for item in items:
            p = Path(item["path"])
            covered_by = None
            for other in items:
                if other["id"] == item["id"]:
                    continue
                op = Path(other["path"])
                if is_within(op, p):
                    covered_by = other
                    break
            if covered_by is not None:
                continue
            covered_count = sum(
                1 for other in items
                if other["id"] != item["id"] and is_within(p, Path(other["path"]))
            )
            out.append({**item, "covered_count": covered_count})
        out.sort(key=lambda x: x["path"].lower())
        return out

    def all_grants(self) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM path_grants WHERE mode = 'read' ORDER BY created_at, id"
                ).fetchall()
            return [self._row(r) for r in rows]
        except sqlite3.Error:
            _logger.exception("[permissions] 读取原始授权失败")
            return []

    def remove(self, grant_id: int) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM path_grants WHERE id = ? AND mode = 'read'",
                                   (int(grant_id),))
                return cur.rowcount > 0
        except (sqlite3.Error, TypeError, ValueError):
            _logger.exception("[permissions] 删除授权失败: %s", grant_id)
            return False

    def clear(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM path_grants WHERE mode = 'read'")
        except sqlite3.Error:
            _logger.exception("[permissions] 清空授权失败")
