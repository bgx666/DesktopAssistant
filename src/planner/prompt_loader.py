"""提示词统一加载（prompts/ 目录的注册表 + 读取/渲染）。

之前提示词散在四处：`prompts/*.md`（系统提示）、`session.py`（运行时注入的
f-string）、`middleware.py`（压缩指令常量）、`tools.py`（工具 docstring）。
本模块把**文件类**提示词统一到 `prompts/` 下，并提供逻辑名 → 文件的注册表
（`PROMPTS`），`prompts/README.md` 由同一张表维护。

- `load(name)`：读原始文本（不渲染）；
- `render(name, **values)`：`str.format` 渲染（占位符缺失直接报错，防静默漏值）；
- 工具 docstring 仍留在 `tools.py`（LangChain 从函数签名/docstring 生成 schema，
  不能外置），在 README 里单独标注。
"""

from __future__ import annotations

from pathlib import Path

from . import config


class PromptNotFound(FileNotFoundError):
    """注册表里没有这个名字，或对应文件不存在。"""


class PromptRenderError(ValueError):
    """模板占位符缺失 / 花括号不合法。"""


#: 逻辑名 → prompts/ 下的相对路径（也是 prompts/README.md 的清单来源）
PROMPTS: dict[str, str] = {
    # ── 系统提示词 ──
    "system": "system.md",
    "workspace": "workspace.md",
    # ── 运行时注入（HumanMessage 进 buffer）──
    "runtime/player_message": "runtime/player_message.md",
    "runtime/heartbeat_wake": "runtime/heartbeat_wake.md",
    "runtime/calendar_due": "runtime/calendar_due.md",
    "runtime/calendar_overdue": "runtime/calendar_overdue.md",
    "runtime/calendar_edit": "runtime/calendar_edit.md",
    "runtime/attachments_header": "runtime/attachments_header.md",
    "runtime/attachment_text": "runtime/attachment_text.md",
    "runtime/attachment_doc": "runtime/attachment_doc.md",
    "runtime/attachment_doc_fail": "runtime/attachment_doc_fail.md",
    "runtime/attachment_image": "runtime/attachment_image.md",
    "runtime/attachment_image_ocr": "runtime/attachment_image_ocr.md",
    "runtime/attachment_image_ocr_fail": "runtime/attachment_image_ocr_fail.md",
    "runtime/attachment_other": "runtime/attachment_other.md",
    "runtime/screenshot": "runtime/screenshot.md",
    "runtime/view_image": "runtime/view_image.md",
    "runtime/limit_reached": "runtime/limit_reached.md",
    "runtime/ask_user_selected": "runtime/ask_user_selected.md",
    "runtime/ask_user_selected_note": "runtime/ask_user_selected_note.md",
    "runtime/ask_user_answer": "runtime/ask_user_answer.md",
    "runtime/ask_user_stop": "runtime/ask_user_stop.md",
    "runtime/ask_user_timeout": "runtime/ask_user_timeout.md",
    "runtime/white_finished": "runtime/white_finished.md",
    # ── 子代理 ──
    "agents/white": "agents/white.md",
    "agents/white_compact": "agents/white_compact.md",
    # ── 压缩（独立摘要模型；中/英 × 叶子/分片/父层 + 骨架/预算）──
    "compress/leaf_zh": "compress/leaf_zh.md",
    "compress/leaf_en": "compress/leaf_en.md",
    "compress/leaf_scope_zh": "compress/leaf_scope_zh.md",
    "compress/leaf_scope_en": "compress/leaf_scope_en.md",
    "compress/leaf_scope_chunk_zh": "compress/leaf_scope_chunk_zh.md",
    "compress/leaf_scope_chunk_en": "compress/leaf_scope_chunk_en.md",
    "compress/parent_zh": "compress/parent_zh.md",
    "compress/parent_en": "compress/parent_en.md",
    "compress/skeleton_zh": "compress/skeleton_zh.md",
    "compress/skeleton_en": "compress/skeleton_en.md",
    "compress/budget_zh": "compress/budget_zh.md",
    "compress/budget_en": "compress/budget_en.md",
}


def prompt_path(prompt_name: str) -> Path:
    """逻辑名 → 绝对路径（不存在则抛 PromptNotFound）。"""
    rel = PROMPTS.get(prompt_name)
    if rel is None:
        raise PromptNotFound(f"未注册的提示词：{prompt_name!r}（见 prompts/README.md）")
    path = config.PROMPTS_DIR / rel
    if not path.is_file():
        raise PromptNotFound(f"提示词文件缺失：{path}")
    return path


def load(prompt_name: str) -> str:
    """读取提示词原文（不渲染）。"""
    return prompt_path(prompt_name).read_text(encoding="utf-8")


def render(prompt_name: str, **values: object) -> str:
    """读取并渲染占位符；缺 key / 花括号不合法 → PromptRenderError。"""
    text = load(prompt_name)
    try:
        return text.format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        raise PromptRenderError(
            f"提示词 {prompt_name!r} 渲染失败：{exc}（占位符：{sorted(values)}）") from exc
