"""消息 content 工具：LLM 消息 content 支持 str 与内容块列表（text/image_url 混合）。

视觉注入后 human 消息 content 形如：
[{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:..."}}]
读取/序列化路径统一走 content_text() 提取纯文本，避免 str(list) 打出 JSON 串污染
上下文与 /history 显示。
"""

from __future__ import annotations


def _block_type(b) -> str:
    if isinstance(b, dict):
        return str(b.get("type", "") or "")
    return str(getattr(b, "type", "") or "")


def _block_text(b) -> str:
    if isinstance(b, dict):
        return str(b.get("text", "") or "")
    return str(getattr(b, "text", "") or "")


def content_text(content) -> str:
    """消息 content → 纯文本（列表块拼接 text 部分，image_url 块忽略）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if _block_type(b) == "text":
                t = _block_text(b)
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return str(content or "")
