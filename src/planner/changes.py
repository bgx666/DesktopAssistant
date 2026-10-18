"""文件变更留底与回退（代码工作区写操作的历史记录）。

- 每次写操作（write/edit/move/delete）记录：工具、路径、变更前内容、变更后内容；
- 内容过大（> MAX_SNAPSHOT_CHARS）或二进制 → `no_snapshot=1`：**该条不可回退**（列表里标注）；
- 回退语义：`rollback_to(change_id)` = **回退到这条之前**——把 id ≥ 该值且未回退的条目
  按**从新到旧**逐步恢复（before 存在则写回，before 缺失即"当时不存在"则删除文件）；
- 路径必须仍在当前工作区内（换过工作区/越界的一律跳过并报错）；黑名单路径拒绝。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from .workspace import is_blocked, is_within

_logger = logging.getLogger("planner.changes")

#: 单侧内容快照上限（超出则只记录元信息、不可回退）
MAX_SNAPSHOT_CHARS = 2_000_000

#: diff 预览：超过该字符数跳过逐行对比（difflib 在大文本上很慢）
MAX_DIFF_CHARS = 200_000
#: diff 预览最多输出行数
MAX_DIFF_LINES = 400


def diff_text(before: str | None, after: str | None, rel: str,
              max_lines: int = MAX_DIFF_LINES) -> str | None:
    """统一的 before→after 对比（unified diff 文本，供工具行/历史展示）。

    - before/after 为 None 分别表示"当时不存在"（新建）/"现在不存在"（删除）；
    - 内容过大（> MAX_DIFF_CHARS）返回 None（调用方给替代说明）；
    - 无差异（或都为空）返回 None。
    """
    import difflib

    b = before or ""
    a = after or ""
    if len(b) > MAX_DIFF_CHARS or len(a) > MAX_DIFF_CHARS:
        return None
    if before is None and after is None:
        return None
    lines = list(difflib.unified_diff(
        b.splitlines(), a.splitlines(),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
    if not lines:
        return None
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"…（diff 过长，仅显示前 {max_lines} 行）"]
    return "\n".join(lines)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    path TEXT NOT NULL,
    rel TEXT NOT NULL,
    before TEXT,
    after TEXT,
    before_missing INTEGER DEFAULT 0,
    after_missing INTEGER DEFAULT 0,
    no_snapshot INTEGER DEFAULT 0,
    reverted INTEGER DEFAULT 0
);
"""


class ChangesDb:
    """data/changes.db：变更历史（单工作区共用一张表，路径里带绝对路径）。"""

    def __init__(self, data_root: Path) -> None:
        self.path = Path(data_root) / "changes.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            with self._conn:
                self._conn.executescript(_CREATE_TABLE)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(self, tool: str, path: str, rel: str,
               before: str | None, after: str | None,
               before_missing: bool = False, after_missing: bool = False,
               no_snapshot: bool = False) -> int:
        """记一条变更，返回 change id。"""
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO changes (ts, tool, path, rel, before, after, "
                    "before_missing, after_missing, no_snapshot, reverted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (time.time(), str(tool), str(path), str(rel), before, after,
                     1 if before_missing else 0, 1 if after_missing else 0,
                     1 if no_snapshot else 0),
                )
                return int(cur.lastrowid)

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "ts": r["ts"], "tool": r["tool"],
            "path": r["path"], "rel": r["rel"],
            "before_missing": bool(r["before_missing"]),
            "after_missing": bool(r["after_missing"]),
            "no_snapshot": bool(r["no_snapshot"]),
            "reverted": bool(r["reverted"]),
            "before_size": len(r["before"] or ""),
            "after_size": len(r["after"] or ""),
        }

    def list(self, limit: int = 200) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM changes ORDER BY id DESC LIMIT ?", (int(limit),))
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def get(self, change_id: int) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM changes WHERE id = ?", (int(change_id),))
            row = cur.fetchone()
            return self._row_to_dict(row) if row else None

    def get_full(self, change_id: int) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM changes WHERE id = ?", (int(change_id),))
            return cur.fetchone()

    def mark_reverted(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            with self._conn:
                self._conn.executemany(
                    "UPDATE changes SET reverted = 1 WHERE id = ?",
                    [(int(i),) for i in ids],
                )

    def clear(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM changes")


def _read_text(path: Path) -> str | None:
    """读文本（utf-8 失败回退 gbk）；不可读返回 None。"""
    if not path.is_file():
        return None
    for enc in ("utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read(MAX_SNAPSHOT_CHARS + 1)
        except (UnicodeDecodeError, OSError):
            continue
    return None


def snapshot_before(path: Path) -> tuple[str | None, bool, bool]:
    """记录前的原内容 → (内容, before_missing, no_snapshot)。"""
    if not path.is_file():
        return None, True, False
    text = _read_text(path)
    if text is None:
        return None, False, True
    if len(text) > MAX_SNAPSHOT_CHARS:
        return None, False, True
    return text, False, False


def rollback_to(db: ChangesDb, change_id: int, workspace: Path | None) -> dict:
    """回退到 `change_id` 之前：恢复 id ≥ change_id 且未回退的变更（新→旧）。

    返回 {reverted: [id...], failed: [{id, reason}...]}。
    """
    reverted: list[int] = []
    failed: list[dict] = []
    with db._lock:
        cur = db._conn.execute(
            "SELECT * FROM changes WHERE id >= ? AND reverted = 0 ORDER BY id DESC",
            (int(change_id),),
        )
        rows = cur.fetchall()
    for r in rows:
        cid = int(r["id"])
        target = Path(r["path"])
        if workspace is not None and not is_within(workspace, target):
            failed.append({"id": cid, "reason": "路径不在当前工作区内"})
            continue
        if is_blocked(target):
            failed.append({"id": cid, "reason": "路径在保护名单内"})
            continue
        if r["no_snapshot"]:
            failed.append({"id": cid, "reason": "内容过大/二进制，未留底，无法回退"})
            continue
        try:
            if r["before_missing"]:
                # 当时不存在 → 删除现在这个文件（已不存在则忽略）
                if target.is_file():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(r["before"] or "")
            reverted.append(cid)
        except OSError as exc:
            failed.append({"id": cid, "reason": f"写入失败：{exc}"})
    db.mark_reverted(reverted)
    _logger.info("[changes] 回退到 #%d 之前：成功 %d 条，失败 %d 条",
                 change_id, len(reverted), len(failed))
    return {"reverted": reverted, "failed": failed}
