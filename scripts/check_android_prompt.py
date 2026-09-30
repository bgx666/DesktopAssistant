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

# 注册表交叉校验用：从 Kotlin 源码里读出**真正注册了的**工具名
ANDROID_SRC = REPO_ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "xiaozhu" / "planner"
TOOLS_DIR = ANDROID_SRC / "core" / "tools"
# 注册表在引擎里（`ConversationEngine`）——后台心跳也要跑同一套工具，所以它不能长在 UI 层。
# 兼容旧的 ChatViewModel 位置，免得改回去时脚本静默失效。
# 注册表在工具工厂里（`ToolRegistryFactory`）——那边能被测试直接复用，
# 不必先把整个引擎（含加密设置）搭起来。依次回退到旧位置，免得改结构时脚本静默失效。
REGISTRY_FILES = [
    ANDROID_SRC / "core" / "agent" / "ToolRegistryFactory.kt",
    ANDROID_SRC / "core" / "agent" / "ConversationEngine.kt",
    ANDROID_SRC / "ui" / "chat" / "ChatViewModel.kt",
]


def _registered_tool_names() -> set[str]:
    """注册表里的每个类 → 它声明的 name。

    做法是笨但可靠的文本解析：先按括号配对取出注册块，再到 core/tools/*.kt 里
    逐类找 `override val name = "..."`。这样"prompt 说有、代码里没有"和
    "代码里有、prompt 没说"都能被抓出来。

    为什么不用正则一把梭：`class X(private val f: () -> Unit)` 这种嵌套括号会让
    `[^)]*` 提前收尾，之前就是这么漏掉 DndTool 的。
    """
    block = None
    for path in REGISTRY_FILES:
        if not path.exists():
            continue
        block = _balanced_block(path.read_text(encoding="utf-8"), "ToolRegistry(", "listOf(")
        if block is not None:
            break
    if block is None:
        return set()
    classes = set(re.findall(r"\b([A-Z]\w*Tool)\(", block))

    declared: dict[str, str] = {}
    for path in TOOLS_DIR.glob("*.kt"):
        text = path.read_text(encoding="utf-8")
        for cls, name in _classes_declaring_tool_name(text):
            declared[cls] = name

    return {declared[c] for c in classes if c in declared}


def _balanced_block(text: str, outer: str, inner: str) -> str | None:
    """取出 `outer(inner( ... ))` 里 inner 的实参文本（按括号配对，不怕嵌套）。"""
    i = text.find(outer)
    if i < 0:
        return None
    start = text.find(inner, i)
    if start < 0:
        return None
    start += len(inner) - 1          # 指向 inner 的左括号
    depth = 0
    for pos in range(start, len(text)):
        ch = text[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:pos]
    return None


def _classes_declaring_tool_name(text: str) -> list[tuple[str, str]]:
    """一个 .kt 文件里所有 `class Xxx ... : Tool` 及其 `override val name`。

    判据是「`: Tool` 出现在 `override val name` 之前」——不按第一个 `{` 切头部，
    因为构造参数里就可能有 lambda（`private val now: () -> ZonedDateTime = { ... }`），
    按 `{` 切会把 `: Tool` 一起切掉（HeartbeatTool / DndTool 都栽在这上面）。
    """
    out: list[tuple[str, str]] = []
    matches = list(re.finditer(r"\bclass\s+(\w+)", text))
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[m.start():end]
        name = re.search(r"override\s+val\s+name\s*=\s*\"([^\"]+)\"", body)
        if not name:
            continue
        if not re.search(r":\s*Tool\b", body[:name.start()]):
            continue
        out.append((m.group(1), name.group(1)))
    return out


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

    registered = _registered_tool_names()
    # prompt 承诺的常驻工具必须真的注册了（否则模型会去调一个不存在的工具）
    not_registered = sorted(set(LOCAL_TOOLS) - registered) if registered else []
    # 注册了却没在 prompt 里说：模型不知道能用，等于白做
    unannounced = sorted(registered - ALLOWED) if registered else []

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

    if not registered:
        ok = False
        print("[FAIL] 没能从注册表源码读出注册的工具（解析失败？）：")
        for path in REGISTRY_FILES:
            print(f"        - {path.relative_to(REPO_ROOT)}")
    else:
        if not_registered:
            ok = False
            print("[FAIL] prompt 承诺了常驻可用、但代码里没注册的工具：")
            for name in not_registered:
                print(f"        - {name}")
        if unannounced:
            ok = False
            print("[FAIL] 代码里注册了、但 prompt 没提的工具：")
            for name in unannounced:
                print(f"        - {name}")

    if ok:
        print(
            f"[OK] prompt 工具名一致：常驻 {len(LOCAL_TOOLS)} 个、连电脑 {len(REMOTE_TOOLS)} 个，"
            f"注册表实测 {len(registered)} 个，无多余、无遗漏、无 PC 残留"
            f"（{PROMPT_PATH.relative_to(REPO_ROOT)}）"
        )
        return 0

    print()
    print(f"     prompt：{PROMPT_PATH.relative_to(REPO_ROOT)}")
    print(f"     工具允许清单：{', '.join(sorted(ALLOWED))}")
    print(f"     注册表实测：{', '.join(sorted(registered)) or '（没读出来）'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
