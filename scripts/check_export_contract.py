#!/usr/bin/env python
"""校验导出文件是否**正好**是安卓导入端认得的那几种形状。

为什么需要它：安卓导入端是按形状解析的（`core/migrate/PcImport.kt`），
而 PC 侧的导出内容取决于上游的存储格式——踩过一次真坑：

    PC 的叶子 `details` 有两种形状——有 future_notes 时是
    `{"messages": [...], "future_notes": [...]}`，没有时是**纯列表**。
    真实数据里 156 个叶子里 116 个是纯列表；导入端最初只认前者，
    结果 74% 的原文会被静默丢掉（用户看到"它记得有这回事但说不出细节"）。

这个脚本把"导出端"与"导入端"的契约变成可机械检查的东西：PC 侧改了导出
（或上游改了存储形状）时，在**电脑上**就红，而不是等用户导完发现记忆少了一半。

用法：

    # 直接读库（等价于跑一次 export_to_android.py，只读不写源库）
    python scripts/check_export_contract.py \\
        --memory-tree D:\\xiaob\\planner-release\\data\\memory_tree.db \\
        --calendar    D:\\xiaob\\planner-release\\data\\planner.db

    # 或检查已有的导出文件
    python scripts/check_export_contract.py --export planner-export.json

退出码 0 = 契约成立；1 = 有导入端不认识的形状（会打印具体是什么）。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

# 与 ExportModels.kt / PcImport.kt 对齐
TOP_KEYS = {"format", "format_version", "exported_at", "source", "counts",
            "nodes", "buffer", "calendar_items", "warnings"}
NODE_KEYS = {"character_id", "id", "level", "summary", "parent_id", "round_start",
             "round_end", "time_start", "time_end", "source_ref", "details",
             "profile", "meta", "is_active", "created_at"}
BUFFER_KEYS = {"character_id", "recent_buffer", "msg_counter", "round",
               "compressed_total", "last_activity_at", "next_heartbeat_at",
               "heartbeat_minutes", "heartbeat_note", "last_player_message_at",
               "reminded_overdue"}
CALENDAR_KEYS = {"id", "date", "time", "kind", "content", "detail", "status",
                 "reminded_at", "created_at", "done_at", "series_id", "repeat",
                 "repeat_until"}
PROFILE_KEYS = {"preferences", "personality", "habits", "goals"}
MESSAGE_TYPES = {"human", "ai", "tool", "system"}
KINDS = {"event", "task"}
STATUSES = {"todo", "done", "cancelled"}
REPEAT_RULES = {"daily", "weekly", "weekdays"}

FORMAT = "planner-android-export"
SUPPORTED_VERSION = 1


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []

    def bad(self, msg: str) -> None:
        self.problems.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def _leaf_messages(details) -> list:
    """叶子原文列表（兼容两种形状：纯列表 / {messages, future_notes}）。"""
    if isinstance(details, list):
        return details
    if isinstance(details, dict):
        inner = details.get("messages")
        return inner if isinstance(inner, list) else []
    return []


def check(payload: dict) -> Report:
    r = Report()

    if payload.get("format") != FORMAT:
        r.bad(f"format 应为 {FORMAT!r}，实际 {payload.get('format')!r}")
    if (payload.get("format_version") or 0) > SUPPORTED_VERSION:
        r.bad(f"format_version={payload.get('format_version')} 超过导入端支持的 {SUPPORTED_VERSION}")

    unknown = set(payload) - TOP_KEYS
    if unknown:
        r.bad(f"顶层多出导入端不认识的字段：{sorted(unknown)}（多出来的会被忽略，确认是有意的）")

    # ── 节点 ────────────────────────────────────────────────
    shapes: dict[str, int] = {}
    leaves_without_details = 0
    for node in payload.get("nodes", []):
        nid = node.get("id")
        unknown = set(node) - NODE_KEYS
        if unknown:
            r.bad(f"节点 {nid} 多出字段 {sorted(unknown)}")
        if not nid or not isinstance(nid, str):
            r.bad(f"节点缺少合法 id：{node!r}")
        if not isinstance(node.get("level"), int):
            r.bad(f"节点 {nid} 的 level 不是整数")

        details = node.get("details")
        if details is None:
            shapes["null"] = shapes.get("null", 0) + 1
            if node.get("level") == 0 and (node.get("summary") or "").strip():
                leaves_without_details += 1
        elif isinstance(details, list):
            shapes["list"] = shapes.get("list", 0) + 1
        elif isinstance(details, dict):
            extra = set(details) - {"messages", "future_notes"}
            if extra:
                r.bad(f"节点 {nid} 的 details 多出字段 {sorted(extra)}（导入端只认 messages/future_notes）")
            if not isinstance(details.get("messages", []), list):
                r.bad(f"节点 {nid} 的 details.messages 不是列表")
            key = "dict:" + ",".join(sorted(details))
            shapes[key] = shapes.get(key, 0) + 1
        else:
            r.bad(f"节点 {nid} 的 details 是 {type(details).__name__}，导入端不认")

        # 叶子原文的每条消息
        for i, msg in enumerate(_leaf_messages(details)):
            if not isinstance(msg, dict):
                r.bad(f"节点 {nid} 第 {i + 1} 条原文不是对象")
                continue
            mtype = msg.get("type")
            if mtype not in MESSAGE_TYPES:
                r.bad(f"节点 {nid} 第 {i + 1} 条原文 type={mtype!r} 不认识（导入端按 type 判角色）")
            has_content = "content" in msg or (
                isinstance(msg.get("data"), dict) and "content" in msg["data"]
            )
            if not has_content:
                r.bad(f"节点 {nid} 第 {i + 1} 条原文既没有 content 也没有 data.content")

        profile = node.get("profile")
        if profile is not None:
            if not isinstance(profile, dict):
                r.bad(f"节点 {nid} 的 profile 不是对象")
            else:
                extra = set(profile) - PROFILE_KEYS
                if extra:
                    r.bad(f"节点 {nid} 的 profile 多出维度 {sorted(extra)}")

    if shapes:
        r.note(f"details 形状分布：{shapes}")
    if leaves_without_details:
        r.note(f"有 {leaves_without_details} 个叶子没有原文（导入端会明确提示用户）")

    # ── buffer ──────────────────────────────────────────────
    buffer = payload.get("buffer")
    if buffer is not None:
        unknown = set(buffer) - BUFFER_KEYS
        if unknown:
            r.bad(f"buffer 多出字段 {sorted(unknown)}")
        recent = buffer.get("recent_buffer")
        if recent is not None:
            if not isinstance(recent, list):
                r.bad("buffer.recent_buffer 不是列表")
            else:
                for i, entry in enumerate(recent):
                    if not isinstance(entry, dict):
                        r.bad(f"buffer 第 {i + 1} 条不是对象")
                        continue
                    etype = entry.get("type")
                    if etype not in MESSAGE_TYPES:
                        r.bad(f"buffer 第 {i + 1} 条 type={etype!r} 不认识")
                    data = entry.get("data")
                    if not isinstance(data, dict):
                        r.bad(f"buffer 第 {i + 1} 条没有 data 对象（导入端按 data 取正文）")
                        continue
                    if "content" not in data:
                        r.bad(f"buffer 第 {i + 1} 条 data 里没有 content")
                    for call in data.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            r.bad(f"buffer 第 {i + 1} 条的 tool_call 不是对象")
                            continue
                        if "name" not in call:
                            r.bad(f"buffer 第 {i + 1} 条的 tool_call 没有 name")
                        if "args" not in call:
                            r.bad(f"buffer 第 {i + 1} 条的 tool_call 没有 args")

    # ── 日历 ────────────────────────────────────────────────
    for item in payload.get("calendar_items", []):
        unknown = set(item) - CALENDAR_KEYS
        if unknown:
            r.bad(f"日历项 {item.get('id')} 多出字段 {sorted(unknown)}")
        if not (item.get("date") or "").strip():
            r.bad(f"日历项 {item.get('id')} 没有日期（导入端会跳过它）")
        if item.get("kind") not in KINDS:
            r.bad(f"日历项 {item.get('id')} kind={item.get('kind')!r} 不认识")
        if item.get("status") not in STATUSES:
            r.bad(f"日历项 {item.get('id')} status={item.get('status')!r} 不认识")
        if item.get("repeat") is not None and item["repeat"] not in REPEAT_RULES:
            r.bad(f"日历项 {item.get('id')} repeat={item.get('repeat')!r} 不认识")

    return r


def load_export(args) -> dict:
    if args.export:
        raw = Path(args.export).read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from export_to_android import export  # noqa: PLC0415  （同目录脚本）

    return export(args.memory_tree, args.calendar, include_details=not args.no_details)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验导出文件是否满足安卓导入端的形状契约")
    ap.add_argument("--memory-tree", type=Path, help="memory_tree.db 路径")
    ap.add_argument("--calendar", type=Path, help="planner.db 路径")
    ap.add_argument("--export", type=Path, help="已有的导出文件（.json 或 .json.gz）")
    ap.add_argument("--no-details", action="store_true", help="按导出时不带原文检查")
    args = ap.parse_args(argv)

    if not args.export and not (args.memory_tree and args.calendar):
        ap.error("要么给 --export，要么同时给 --memory-tree 与 --calendar")

    try:
        payload = load_export(args)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 读不出导出内容：{exc}", file=sys.stderr)
        return 1

    report = check(payload)
    counts = payload.get("counts") or {}
    print(f"[check] 节点 {counts.get('nodes', len(payload.get('nodes', [])))}、"
          f"日历 {counts.get('calendar_items', len(payload.get('calendar_items', [])))}、"
          f"buffer {'有' if payload.get('buffer') else '无'}")
    for note in report.notes:
        print(f"  [info] {note}")

    if report.problems:
        print()
        print("[FAIL] 下面这些形状安卓导入端不认（会导致记忆/日程静默丢失）：")
        for problem in report.problems[:20]:
            print(f"        - {problem}")
        if len(report.problems) > 20:
            print(f"        …… 另有 {len(report.problems) - 20} 条")
        print()
        print("     导入端：android/app/src/main/java/com/xiaozhu/planner/core/migrate/PcImport.kt")
        print("     导出端：scripts/export_to_android.py")
        return 1

    print("[OK] 导出形状与安卓导入端契约一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
