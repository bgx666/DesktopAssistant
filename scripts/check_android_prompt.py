#!/usr/bin/env python
"""校验安卓版 system prompt 里提到的工具名：不许多、不许漏。

为什么需要它：安卓版的 prompt 是从 PC 版裁剪来的，而 PC 版有 13 个工具、安卓分成
「常驻」与「连电脑才有」两类。裁剪时最容易犯的错是**留下安卓不存在的工具**（模型会去
调它），或者**漏掉某个真实工具**（模型不知道能用）。这个脚本把这两件事变成可机械检查。

用法：
    python scripts/check_android_prompt.py

退出码 0 = 通过；1 = 有问题（会打印具体是哪些）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "android" / "app" / "src" / "main" / "assets" / "prompts" / "system.md"

# A 类：手机上常驻（不依赖电脑）。PC 版 13 个里的 10 个 + 安卓新增的 search_memory
LOCAL_TOOLS = [
    "add_schedule",
    "list_schedule",
    "update_schedule",
    "cancel_schedule",
    "heartbeat",
    "continue_speaking",
    "set_do_not_disturb",
    "explore_memory_tree",
    "search_memory",
    "web_search",
    "fetch_web",
]

# B 类：只有连上电脑时才会出现在工具列表里
REMOTE_TOOLS = ["list_dir", "read_file", "capture_screen"]

# PC 版有、但安卓**不存在**的东西：prompt 里出现即为错误（防止整体复制带进来）
PC_ONLY_BANNED = [
    "floating bubble",  # 悬浮球
    "electron",
    "18771",
    "18772",
    "18766",
    "planner-release",
]

ALLOWED = set(LOCAL_TOOLS) | set(REMOTE_TOOLS)


def main() -> int:
    if not PROMPT_PATH.exists():
        print(f"[FAIL] 找不到 prompt：{PROMPT_PATH}")
        return 1

    text = PROMPT_PATH.read_text(encoding="utf-8")

    # 只挑"看起来像工具名"的反引号片段：在允许清单里，或含下划线
    backticked = set(re.findall(r"`([a-z][a-z0-9_]{2,})`", text))
    tool_like = {name for name in backticked if name in ALLOWED or "_" in name}

    unknown = sorted(tool_like - ALLOWED)
    missing_local = sorted(set(LOCAL_TOOLS) - backticked)
    banned_hits = sorted(kw for kw in PC_ONLY_BANNED if kw.lower() in text.lower())

    ok = True

    if unknown:
        ok = False
        print("[FAIL] prompt 里出现了不在允许清单里的工具名（模型会去调不存在的工具）：")
        for name in unknown:
            print(f"        - {name}")

    if missing_local:
        ok = False
        print("[FAIL] 这些常驻工具没在 prompt 里被提到（模型不知道能用）：")
        for name in missing_local:
            print(f"        - {name}")

    if banned_hits:
        ok = False
        print("[FAIL] prompt 里残留了 PC 专有内容（安卓上不存在）：")
        for kw in banned_hits:
            print(f"        - {kw}")

    if ok:
        print(
            f"[OK] prompt 工具名一致：常驻 {len(LOCAL_TOOLS)} 个、连电脑 {len(REMOTE_TOOLS)} 个，"
            f"无多余、无遗漏、无 PC 残留（{PROMPT_PATH.relative_to(REPO_ROOT)}）"
        )
        return 0

    print()
    print(f"     prompt：{PROMPT_PATH.relative_to(REPO_ROOT)}")
    print(f"     工具允许清单：{', '.join(sorted(ALLOWED))}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
