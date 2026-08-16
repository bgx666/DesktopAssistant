"""用户设置：data/settings.json（随 PLANNER_DATA_ROOT 隔离，release 与开发版各自独立）。

设置项：
- press_ms            悬浮球长按判定毫秒
- compress_trigger    对话积累多少条消息触发压缩
- compress_keep       压缩后保留多少条原始消息（须 < trigger）
- compact_threshold   某层节点数达到多少触发向上合并
- compact_factor      每次合并几个节点（须 < threshold）
- llm_api_key/base_url/model  LLM 配置（留空 = 使用环境变量 / .env）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_logger = logging.getLogger("planner.settings")

DEFAULT_SETTINGS: dict = {
    "press_ms": 200,
    "compress_trigger": 60,
    "compress_keep": 20,
    "compact_threshold": 8,
    "compact_factor": 4,
    "llm_api_key": "",
    "llm_base_url": "",
    "llm_model": "",
    "tts_enabled": True,
    "tts_engine": "",          # 留空 = 跟随环境变量 PLANNER_TTS_ENGINE；local / mimo / cloud
    "tts_voice": "zf_001",
}

# 数值范围护栏（前端输入 + POST 双重校验）
_LIMITS = {
    "press_ms": (50, 5000),
    "compress_trigger": (20, 500),
    "compress_keep": (5, 200),
    "compact_threshold": (3, 50),
    "compact_factor": (2, 10),
}


def settings_path(data_root: Path) -> Path:
    return Path(data_root) / "settings.json"


def load_settings(data_root: Path) -> dict:
    """读取设置（合并默认值，损坏文件静默回退默认）。"""
    s = dict(DEFAULT_SETTINGS)
    try:
        p = settings_path(data_root)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULT_SETTINGS:
                    if k in raw:
                        s[k] = raw[k]
    except Exception:
        _logger.warning("[settings] 读取失败，使用默认值")
    return s


def save_settings(data_root: Path, updates: dict) -> dict:
    """校验并保存设置，返回合并后的完整设置。校验失败抛 ValueError。"""
    merged = load_settings(data_root)
    for k, v in (updates or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k in _LIMITS:
            lo, hi = _LIMITS[k]
            try:
                num = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} 必须是数字")
            if not (lo <= num <= hi):
                raise ValueError(f"{k} 需在 {lo}~{hi} 之间")
            merged[k] = num
        else:
            # 布尔字段保真（tts_enabled）；其余字符串化（空值归一为 ""）
            merged[k] = v if isinstance(v, bool) else str(v or "")
    # 交叉约束：keep < trigger；factor < threshold
    if merged["compress_keep"] >= merged["compress_trigger"]:
        raise ValueError("压缩后保留条数必须小于触发条数")
    if merged["compact_factor"] >= merged["compact_threshold"]:
        raise ValueError("合并个数必须小于触发阈值")
    try:
        p = settings_path(data_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        _logger.warning("[settings] 写入失败")
        raise ValueError("设置写入失败")
    return merged
