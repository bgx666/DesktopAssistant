"""文档解析：PDF（PyMuPDF）/ Word（python-docx）→ 纯文本。

用户拖入的文档解析后落盘到 data/attachments/{hash}.txt（随 PLANNER_DATA_ROOT
隔离），注入消息给前 8000 字预览 + txt 路径——模型需要完整内容时用
read_file 工具分段读取，大文档不撑爆上下文。

lazy import：解析库未安装时优雅降级（返回 None，注入仅路径提示）。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_logger = logging.getLogger("planner.fileparse")

try:
    import fitz  # PyMuPDF

    _HAS_FITZ = True
except Exception:
    fitz = None
    _HAS_FITZ = False

try:
    import docx  # python-docx

    _HAS_DOCX = True
except Exception:
    docx = None
    _HAS_DOCX = False

_PREVIEW_CHARS = 8000      # 注入消息里的预览长度
_MAX_PARSE_CHARS = 500_000  # 解析文本上限（超长截断，防内存失控）


def parse_pdf_text(path: Path) -> str | None:
    """提取 PDF 全部文本（只读）。失败/空返回 None。"""
    if not _HAS_FITZ:
        return None
    try:
        doc = fitz.open(str(path))
        parts = []
        for page in doc:
            parts.append(page.get_text())
        doc.close()
        text = "\n".join(parts).strip()
        return text[: _MAX_PARSE_CHARS] or None
    except Exception:
        _logger.exception("[fileparse] PDF 解析失败: %s", path)
        return None


def parse_docx_text(path: Path) -> str | None:
    """提取 Word(.docx) 段落文本。"""
    if not _HAS_DOCX:
        return None
    try:
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        text = "\n".join(parts).strip()
        return text[: _MAX_PARSE_CHARS] or None
    except Exception:
        _logger.exception("[fileparse] docx 解析失败: %s", path)
        return None


def parse_file(path: str) -> str | None:
    """按扩展名解析文档 → 纯文本；不支持/失败返回 None。"""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        ext = p.suffix.lower()
        if ext == ".pdf":
            return parse_pdf_text(p)
        if ext == ".docx":
            return parse_docx_text(p)
        return None
    except Exception:
        return None


def save_attachment_text(data_root: Path, source_path: str, text: str) -> Path:
    """解析文本落盘 data/attachments/{hash}.txt，返回路径（随 data_root 隔离）。"""
    h = hashlib.sha1(source_path.encode("utf-8", "replace")).hexdigest()[:16]
    d = Path(data_root) / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{h}.txt"
    out.write_text(text, encoding="utf-8")
    return out
