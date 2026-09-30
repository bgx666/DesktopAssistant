"""导出契约检查器的测试。

这个检查器的价值全在"会不会漏"上，所以正向用**真实的导出形状**、
反向把导入端不认的每种形状都造一遍——漏一种，用户就会在手机上发现
记忆少了一半（而且很难复盘）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_export_contract import check, load_export  # noqa: E402


def payload(**overrides) -> dict:
    base = {
        "format": "planner-android-export",
        "format_version": 1,
        "exported_at": "2026-09-30T10:00:00+08:00",
        "source": {"memory_tree": "x"},
        "counts": {"nodes": 1},
        "nodes": [],
        "buffer": None,
        "calendar_items": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_真实形状通过() -> None:
    """与生产数据一致的一组形状：两种 details、两种消息形状、tool_calls。"""
    report = check(payload(
        nodes=[
            # 有 future_notes 的叶子
            {"id": "node0_001", "level": 0, "summary": "摘要", "parent_id": None,
             "round_start": 0, "round_end": 39, "time_start": "2026-09-28 09:00:00",
             "time_end": "2026-09-28 09:40:00", "source_ref": None,
             "details": {"messages": [{"type": "human", "data": {"content": "在吗"}},
                                      {"type": "ai", "content": "在"}],
                         "future_notes": ["后来改到周一"]},
             "profile": {"preferences": ["直接"], "personality": [], "habits": [], "goals": []},
             "meta": {"schema_version": 1}, "is_active": 1, "created_at": 1759020000},
            # 没有 future_notes 的叶子：details 是纯列表（真实数据里占多数）
            {"id": "node0_002", "level": 0, "summary": "另一段",
             "details": [{"type": "human", "data": {"content": "投了中电"}}]},
            # 父节点：details 为 null
            {"id": "node1_001", "level": 1, "summary": "上卷", "details": None},
        ],
        buffer={
            "character_id": "assistant",
            "recent_buffer": [
                {"type": "human", "data": {"content": "在吗", "type": "human"}},
                {"type": "ai", "data": {"content": "", "tool_calls": [
                    {"name": "heartbeat", "args": {"minutes": 230}, "id": "call_1",
                     "type": "tool_call"}]}},
                {"type": "tool", "data": {"content": "好", "tool_call_id": "call_1",
                                          "name": "heartbeat", "type": "tool"}},
            ],
            "msg_counter": 42, "round": 7, "compressed_total": 40,
            "last_activity_at": 1759200000.5, "next_heartbeat_at": 1759203600.0,
            "heartbeat_minutes": 60.0, "heartbeat_note": "看看进度",
            "last_player_message_at": 1759199000.0, "reminded_overdue": [],
        },
        calendar_items=[
            {"id": 3, "date": "2026-09-30", "time": "20:00", "kind": "event",
             "content": "复习", "detail": "", "status": "todo", "reminded_at": None,
             "created_at": "2026-09-28 09:00:00", "done_at": None,
             "series_id": None, "repeat": None, "repeat_until": None},
            {"id": 4, "date": "2026-09-30", "time": "08:00", "kind": "task",
             "content": "吃药", "detail": "", "status": "done",
             "reminded_at": "2026-09-30 08:00", "created_at": "2026-09-28 09:00:00",
             "done_at": "2026-09-30 08:05:00", "series_id": "abc",
             "repeat": "daily", "repeat_until": "2027-09-30"},
        ],
    ))
    assert report.problems == []
    assert any("details 形状分布" in n for n in report.notes)


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("format 不对", {"format": "something-else"}),
        ("版本过高", {"format_version": 99}),
        ("顶层多字段", {"extra_field": 1}),
        ("details 是字符串", {"nodes": [{"id": "n", "level": 0, "summary": "s", "details": "oops"}]}),
        ("叶子消息缺 type", {"nodes": [{"id": "n", "level": 0, "summary": "s",
                                    "details": [{"content": "x"}]}]}),
        ("叶子消息缺 content", {"nodes": [{"id": "n", "level": 0, "summary": "s",
                                      "details": [{"type": "human"}]}]}),
        ("叶子消息 type 不认识", {"nodes": [{"id": "n", "level": 0, "summary": "s",
                                       "details": [{"type": "alien", "content": "x"}]}]}),
        ("details 多字段", {"nodes": [{"id": "n", "level": 0, "summary": "s",
                                   "details": {"messages": [], "future_notes": [],
                                               "unknown": 1}}]}),
        ("profile 多维度", {"nodes": [{"id": "n", "level": 0, "summary": "s",
                                   "profile": {"preferences": [], "mood": []}}]}),
        ("节点没有 id", {"nodes": [{"level": 0, "summary": "s"}]}),
        ("level 不是整数", {"nodes": [{"id": "n", "level": "0", "summary": "s"}]}),
        ("buffer 条目没有 data", {"buffer": {"recent_buffer": [{"type": "human"}]}}),
        ("buffer 条目 type 不认识", {"buffer": {"recent_buffer": [{"type": "robot",
                                                            "data": {"content": "x"}}]}}),
        ("buffer 多字段", {"buffer": {"unknown": 1}}),
        ("tool_call 缺 args", {"buffer": {"recent_buffer": [
            {"type": "ai", "data": {"content": "", "tool_calls": [{"name": "x"}]}}]}}),
        ("tool_call 缺 name", {"buffer": {"recent_buffer": [
            {"type": "ai", "data": {"content": "", "tool_calls": [{"args": {}}]}}]}}),
        ("日历 kind 不认识", {"calendar_items": [{"id": 1, "date": "2026-09-30",
                                            "kind": "alarm", "status": "todo"}]}),
        ("日历 status 不认识", {"calendar_items": [{"id": 1, "date": "2026-09-30",
                                              "kind": "event", "status": "maybe"}]}),
        ("日历没有日期", {"calendar_items": [{"id": 1, "date": "", "kind": "event",
                                          "status": "todo"}]}),
        ("日历 repeat 不认识", {"calendar_items": [{"id": 1, "date": "2026-09-30",
                                              "kind": "event", "status": "todo",
                                              "repeat": "monthly"}]}),
        ("日历多字段", {"calendar_items": [{"id": 1, "date": "2026-09-30", "kind": "event",
                                        "status": "todo", "unknown": 1}]}),
    ],
)
def test_导入端不认的形状都会红(name: str, overrides: dict) -> None:
    report = check(payload(**overrides))
    assert report.problems, f"{name} 应该被检查出来"


def test_叶子没有原文时给出提示而不是报错() -> None:
    """`--no-details` 导出是**允许**的，只是要提醒用户原文没了。"""
    report = check(payload(nodes=[{"id": "node0_001", "level": 0, "summary": "有摘要",
                                   "details": None}]))
    assert report.problems == []
    assert any("没有原文" in n for n in report.notes)


def test_读得出_plain_与_gzip_两种导出文件(tmp_path: Path) -> None:
    import gzip

    content = json.dumps(payload(), ensure_ascii=False).encode("utf-8")
    plain = tmp_path / "export.json"
    plain.write_bytes(content)
    gz = tmp_path / "export.json.gz"
    gz.write_bytes(gzip.compress(content))

    class Args:
        export = plain
        memory_tree = None
        calendar = None
        no_details = False

    assert load_export(Args())["format"] == "planner-android-export"
    Args.export = gz
    assert load_export(Args())["format"] == "planner-android-export"


def test_真实导出脚本的产物通过契约(tmp_path: Path) -> None:
    """端到端：造两个源库 → export_to_android 导出 → 契约检查必须绿。"""
    import sqlite3

    from export_to_android import export

    mt = tmp_path / "memory_tree.db"
    conn = sqlite3.connect(mt)
    conn.executescript(
        """
        CREATE TABLE nodes (
            character_id TEXT NOT NULL, id TEXT NOT NULL, level INTEGER NOT NULL,
            summary TEXT NOT NULL, parent_id TEXT, round_start INTEGER, round_end INTEGER,
            time_start TEXT, time_end TEXT, source_ref TEXT, details TEXT, profile TEXT,
            meta TEXT, is_active INTEGER DEFAULT 1, created_at INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE buffer_state (
            character_id TEXT PRIMARY KEY, recent_buffer TEXT, msg_counter INTEGER,
            round INTEGER, last_activity_at REAL, reminded_overdue TEXT,
            compressed_total INTEGER, next_heartbeat_at REAL, heartbeat_minutes REAL,
            heartbeat_note TEXT, last_player_message_at REAL
        );
        """
    )
    # 一个带 future_notes 的叶子 + 一个纯列表的叶子（两种形状都要能过）
    conn.execute(
        "INSERT INTO nodes (character_id, id, level, summary, details, profile) VALUES (?,?,?,?,?,?)",
        ("assistant", "node0_001", 0, "摘要一",
         json.dumps({"messages": [{"type": "human", "data": {"content": "在吗"}}],
                     "future_notes": ["后来改了"]}, ensure_ascii=False),
         json.dumps({"preferences": ["直接"]}, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO nodes (character_id, id, level, summary, details) VALUES (?,?,?,?,?)",
        ("assistant", "node0_002", 0, "摘要二",
         json.dumps([{"type": "ai", "data": {"content": "在"}}], ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO buffer_state (character_id, recent_buffer, msg_counter, round) VALUES (?,?,?,?)",
        ("assistant", json.dumps([{"type": "human", "data": {"content": "在吗"}}]), 2, 1),
    )
    conn.commit()
    conn.close()

    cal = tmp_path / "planner.db"
    conn = sqlite3.connect(cal)
    conn.executescript(
        """
        CREATE TABLE calendar_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT,
            kind TEXT NOT NULL DEFAULT 'event', content TEXT NOT NULL, detail TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo', reminded_at TEXT,
            created_at TEXT DEFAULT (datetime('now', '+8 hours')), done_at TEXT,
            series_id TEXT, repeat TEXT, repeat_until TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO calendar_items (date, time, kind, content) VALUES (?,?,?,?)",
        ("2026-09-30", "20:00", "event", "复习"),
    )
    conn.commit()
    conn.close()

    data = export(mt, cal, include_details=True)
    report = check(data)
    assert report.problems == []
