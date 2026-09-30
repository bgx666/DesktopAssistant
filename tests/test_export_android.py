"""安卓迁移导出脚本（scripts/export_to_android.py）的测试。

用**与真实 schema 一致**的临时库（不是 mock）建两个源库，验证三件事：
1. 导出内容正确：节点/画像/原文/日历字段都在、JSON 列被还原成对象；
2. `--no-details` 只去掉原文，其余照旧；
3. **源库不被修改**（迁移工具最要紧的约束——只读）。

schema 逐字抄自 `src/planner/memory/sqlite_memory_tree.py::_CREATE_TABLES`
与 `src/planner/store/calendar_db.py`；故意不调用源码里的建表函数，这样两边漂移时本测试会红。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = REPO_ROOT / "scripts" / "export_to_android.py"

MEMORY_TREE_SCHEMA = """
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

CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT,
    kind TEXT NOT NULL DEFAULT 'event',
    content TEXT NOT NULL,
    detail TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    reminded_at TEXT,
    created_at TEXT DEFAULT (datetime('now', '+8 hours')),
    done_at TEXT,
    series_id TEXT,
    repeat TEXT,
    repeat_until TEXT
);
"""


@pytest.fixture()
def exporter():
    """按路径加载脚本模块（scripts/ 不是包，没法直接 import）。"""
    spec = importlib.util.spec_from_file_location("export_to_android", EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def source_dbs(tmp_path, exporter):
    """建两个源库并塞入真实形状的数据，返回 (memory_tree_path, calendar_path)。"""
    mt_path = tmp_path / "memory_tree.db"
    cal_path = tmp_path / "planner.db"

    conn = sqlite3.connect(mt_path)
    conn.executescript(MEMORY_TREE_SCHEMA)
    details = {
        "messages": [
            {"type": "human", "data": {"content": "明天下午三点提醒我开会"}},
            {"type": "ai", "data": {"content": "行，记上了。"}},
        ],
        "future_notes": ["原定 15:00，用户后来改成 16:00"],
    }
    profile = {"preferences": ["喜欢晚上学习"], "personality": [], "habits": [], "goals": []}
    conn.execute(
        "INSERT INTO nodes (character_id, id, level, summary, parent_id, round_start, round_end,"
        " time_start, time_end, source_ref, details, profile, meta, is_active)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("assistant", "node0_001", 0, "聊了开会安排", None, 0, 39,
         "2026-09-13 20:00:00", "2026-09-13 20:30:00", None,
         json.dumps(details, ensure_ascii=False), json.dumps(profile, ensure_ascii=False),
         json.dumps({"schema_version": 1}, ensure_ascii=False), 1),
    )
    conn.execute(
        "INSERT INTO nodes (character_id, id, level, summary, parent_id, round_start, round_end,"
        " details, is_active) VALUES (?,?,?,?,?,?,?,?,?)",
        ("assistant", "node1_001", 1, "上卷摘要", None, 0, 159, None, 1),
    )
    conn.execute(
        "INSERT INTO buffer_state (character_id, recent_buffer, msg_counter, round,"
        " compressed_total, reminded_overdue, heartbeat_minutes)"
        " VALUES (?,?,?,?,?,?,?)",
        ("assistant", json.dumps([{"type": "human", "data": {"content": "在吗"}}]), 12, 3, 40, "[]", 60.0),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(cal_path)
    conn.executescript(CALENDAR_SCHEMA)
    conn.execute(
        "INSERT INTO calendar_items (date, time, kind, content, detail, status)"
        " VALUES (?,?,?,?,?,?)",
        ("2026-09-14", "15:00", "event", "开会", "带上笔记本", "todo"),
    )
    conn.execute(
        "INSERT INTO calendar_items (date, time, kind, content, status, series_id, repeat)"
        " VALUES (?,?,?,?,?,?,?)",
        ("2026-09-14", "08:00", "task", "吃药", "todo", "series_1", "daily"),
    )
    conn.commit()
    conn.close()

    return mt_path, cal_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_roundtrip(exporter, source_dbs):
    mt, cal = source_dbs
    payload = exporter.export(mt, cal)

    assert payload["format"] == "planner-android-export"
    assert payload["format_version"] == 1
    assert payload["warnings"] == []
    assert payload["counts"]["nodes"] == 2
    assert payload["counts"]["nodes_with_details"] == 1
    assert payload["counts"]["calendar_items"] == 2
    assert payload["counts"]["buffer_present"] is True
    assert payload["source"]["character_ids"] == ["assistant"]

    leaf = next(n for n in payload["nodes"] if n["id"] == "node0_001")
    # JSON 列被还原成对象，不是字符串
    assert isinstance(leaf["details"], dict)
    assert leaf["details"]["messages"][0]["data"]["content"] == "明天下午三点提醒我开会"
    assert leaf["details"]["future_notes"] == ["原定 15:00，用户后来改成 16:00"]
    assert isinstance(leaf["profile"], dict)
    assert leaf["profile"]["preferences"] == ["喜欢晚上学习"]
    assert leaf["meta"] == {"schema_version": 1}
    assert leaf["time_start"] == "2026-09-13 20:00:00"
    assert leaf["is_active"] == 1

    parent = next(n for n in payload["nodes"] if n["id"] == "node1_001")
    assert parent["details"] is None

    # buffer 的 JSON 也还原了
    assert isinstance(payload["buffer"]["recent_buffer"], list)
    assert payload["buffer"]["recent_buffer"][0]["data"]["content"] == "在吗"
    assert payload["buffer"]["heartbeat_minutes"] == 60.0

    # 日历字段完整（安卓侧要按这些字段重建重复系列）
    meet = next(i for i in payload["calendar_items"] if i["content"] == "开会")
    assert (meet["date"], meet["time"], meet["kind"], meet["status"]) == ("2026-09-14", "15:00", "event", "todo")
    assert meet["detail"] == "带上笔记本"
    med = next(i for i in payload["calendar_items"] if i["content"] == "吃药")
    assert med["series_id"] == "series_1" and med["repeat"] == "daily"


def test_export_no_details(exporter, source_dbs):
    mt, cal = source_dbs
    payload = exporter.export(mt, cal, include_details=False)
    leaf = next(n for n in payload["nodes"] if n["id"] == "node0_001")
    assert leaf["details"] is None, "no-details 时应丢弃原文"
    assert payload["counts"]["nodes_with_details"] == 0
    # 其余字段照旧（画像不该受影响）
    assert leaf["profile"]["preferences"] == ["喜欢晚上学习"]
    assert payload["counts"]["calendar_items"] == 2


def test_export_never_modifies_sources(exporter, source_dbs):
    mt, cal = source_dbs
    before = (_digest(mt), _digest(cal))
    exporter.export(mt, cal)
    exporter.export(mt, cal, include_details=False)
    assert (_digest(mt), _digest(cal)) == before, "导出必须只读，不许改动源库"


def test_cli_writes_json_file(exporter, source_dbs, tmp_path):
    mt, cal = source_dbs
    out = tmp_path / "planner-export.json"
    rc = exporter.main([
        "--memory-tree", str(mt), "--calendar", str(cal), "--out", str(out),
    ])
    assert rc == 0
    assert out.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["counts"]["nodes"] == 2
    assert written["counts"]["calendar_items"] == 2


def test_cli_missing_file_returns_nonzero(exporter, tmp_path):
    rc = exporter.main([
        "--memory-tree", str(tmp_path / "nope.db"),
        "--calendar", str(tmp_path / "nope2.db"),
        "--out", str(tmp_path / "out.json"),
    ])
    assert rc == 1
    assert not (tmp_path / "out.json").exists()


def test_cli_gzip_output(exporter, source_dbs, tmp_path):
    import gzip

    mt, cal = source_dbs
    out = tmp_path / "planner-export.json"
    rc = exporter.main([
        "--memory-tree", str(mt), "--calendar", str(cal), "--out", str(out), "--gzip",
    ])
    assert rc == 0
    gz = tmp_path / "planner-export.json.gz"
    assert gz.exists()
    payload = json.loads(gzip.open(gz, "rt", encoding="utf-8").read())
    assert payload["counts"]["nodes"] == 2
