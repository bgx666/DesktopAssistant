#!/usr/bin/env python
"""把小助（PC 版）的旧数据导出成一个 JSON，供安卓版一次性导入。

为什么是 CLI 而不是 HTTP 端点：安卓版是主力大脑、电脑只当工具，而迁移是**一次性**动作。
一个只读脚本最简单也最安全——不启动服务、不监听端口、不写源库（连接始终 `mode=ro`）。

导出内容（安卓侧需要的最小集）：

- `nodes`           记忆树全部节点（含叶子 `details` 里的原文；`--no-details` 可跳过以减小体积）
- `buffer`          `buffer_state` 那一行（当前上下文 + 心跳/压缩等运行状态）
- `calendar_items`  日历全部条目（含重复系列的 `series_id` / `repeat` / `repeat_until`）

用法：

    python scripts/export_to_android.py \\
        --memory-tree D:\\xiaob\\planner-release\\data\\memory_tree.db \\
        --calendar    D:\\xiaob\\planner-release\\data\\planner.db \\
        --out         D:\\xiaob\\planner-export.json

    # 体积太大时（原文很多），去掉原文并压缩：
    python scripts/export_to_android.py --memory-tree ... --calendar ... --out ... --no-details --gzip

导出完成后把生成的 JSON 传到手机，由 App 的「导入旧数据」入口读入（带二次确认）。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

FORMAT = "planner-android-export"
FORMAT_VERSION = 1

# 需要从 JSON 字符串还原成对象的列（还原失败则保留原始字符串并记入 warnings）
JSON_COLUMNS = {
    "nodes": ("details", "profile", "meta"),
}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开：优先 mode=ro（会正确重放 WAL）；失败再用 immutable=1 兜底。"""
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.Error:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True)
        print(f"  [warn] {db_path.name} 无法以 mode=ro 打开，改用 immutable=1 "
              f"（未 checkpoint 的 WAL 内容可能读不到）", file=sys.stderr)
        return conn


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql).fetchall()]


def export(
    memory_tree_path: Path,
    calendar_path: Path,
    *,
    include_details: bool = True,
) -> dict:
    """读取两个源库，返回可直接 json.dump 的 dict。不修改任何源库。"""
    warnings: list[str] = []

    mt = _connect_ro(memory_tree_path)
    try:
        nodes = _rows(mt, "SELECT * FROM nodes ORDER BY level, round_start, id")
        buffer_rows = _rows(mt, "SELECT * FROM buffer_state")
    finally:
        mt.close()

    cal = _connect_ro(calendar_path)
    try:
        items = _rows(cal, "SELECT * FROM calendar_items ORDER BY date, time, id")
    finally:
        cal.close()

    for node in nodes:
        for col in JSON_COLUMNS["nodes"]:
            raw = node.get(col)
            if raw in (None, ""):
                continue
            if col == "details" and not include_details:
                node[col] = None
                continue
            try:
                node[col] = json.loads(raw)
            except (TypeError, ValueError):
                warnings.append(f"node {node.get('id')}: 列 {col} 不是合法 JSON，已按原始字符串导出")

    buffer = buffer_rows[0] if buffer_rows else None
    if buffer is not None:
        try:
            if buffer.get("recent_buffer"):
                buffer["recent_buffer"] = json.loads(buffer["recent_buffer"])
            if buffer.get("reminded_overdue"):
                buffer["reminded_overdue"] = json.loads(buffer["reminded_overdue"])
        except (TypeError, ValueError):
            warnings.append("buffer_state: recent_buffer/reminded_overdue 解析失败，已按原始字符串导出")

    character_ids = {n.get("character_id") for n in nodes if n.get("character_id")}
    if buffer and buffer.get("character_id"):
        character_ids.add(buffer["character_id"])

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "memory_tree": str(memory_tree_path),
            "calendar": str(calendar_path),
            "character_ids": sorted(character_ids),
        },
        "counts": {
            "nodes": len(nodes),
            "nodes_with_details": sum(
                1 for n in nodes if isinstance(n.get("details"), dict)
            ),
            "buffer_present": buffer is not None,
            "calendar_items": len(items),
        },
        "nodes": nodes,
        "buffer": buffer,
        "calendar_items": items,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出自小助 PC 版的数据，供安卓版导入")
    ap.add_argument("--memory-tree", required=True, type=Path,
                    help="memory_tree.db 的路径（nodes + buffer_state）")
    ap.add_argument("--calendar", required=True, type=Path,
                    help="planner.db 的路径（calendar_items）")
    ap.add_argument("--out", default=Path("planner-export.json"), type=Path,
                    help="输出文件（默认 ./planner-export.json）")
    ap.add_argument("--no-details", action="store_true",
                    help="不导出叶子里保存的对话原文（体积小很多，但安卓侧就看不到原话了）")
    ap.add_argument("--gzip", action="store_true",
                    help="输出 .gz（建议配合 --no-details 之外的场景）")
    ap.add_argument("--pretty", action="store_true", help="缩进输出（便于人工查看）")
    args = ap.parse_args(argv)

    for path in (args.memory_tree, args.calendar):
        if not path.exists():
            print(f"[FAIL] 找不到文件：{path}", file=sys.stderr)
            return 1

    print(f"[export] 读 {args.memory_tree}")
    print(f"[export] 读 {args.calendar}")
    payload = export(args.memory_tree, args.calendar,
                     include_details=not args.no_details)

    out: Path = args.out
    if args.gzip and out.suffix != ".gz":
        out = out.with_suffix(out.suffix + ".gz")

    if out.suffix == ".gz":
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False,
                      indent=1 if args.pretty else None,
                      separators=None if args.pretty else (",", ":"))
    else:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False,
                      indent=1 if args.pretty else None,
                      separators=None if args.pretty else (",", ":"))

    size_mb = out.stat().st_size / 1024 / 1024
    c = payload["counts"]
    print(f"[export] 完成：{out.resolve()}  ({size_mb:.1f} MB)")
    print(f"[export] 节点 {c['nodes']}（含原文 {c['nodes_with_details']}）、"
          f"日历 {c['calendar_items']} 项、buffer {'有' if c['buffer_present'] else '无'}")
    for w in payload["warnings"]:
        print(f"  [warn] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
