"""用户设置：data/settings.json（随 PLANNER_DATA_ROOT 隔离，release 与开发版各自独立）。

设置项：
- press_ms            悬浮球长按判定毫秒
- compress_keep       压缩后保留多少条原始消息
- compress_token_budget  原始消息估算 token 超过该值时触发叶子压缩（**唯一触发条件**）
- compress_chunk_tokens  批次估算 token 超过该大小 → 切分片逐片压缩（叶子更细、探索回原文更容易；0 = 关闭分片整批压；默认 3 万）
- compact_threshold   某层节点数达到多少触发向上合并
- compact_factor      每次合并几个节点（须 < threshold）
- node_token_budget   压缩节点整节点 token 预算（summary 占大头）
- workspace_root      代码工作区绝对路径（空 = 代码能力关闭）
- workspace_cwd       当前工作目录（工作区子目录；空 = 工作区根）
- workspace_recent    最近目录列表（≤8，去重，最近在前）
- permission_mode     权限模式：ask（写都审批，默认）/ auto（当前目录内写自动放行）/ read_only（禁写）
- approval_timeout_seconds  审批等待上限秒数
- code_read_outside_workspace  是否允许发起「工作区外读」审批
- thinking_mode       思考开关：adaptive（自适应，默认）/ enabled（总是思考）/ disabled（关闭）
- reasoning_effort   思考强度：low / high（默认）/ max（关闭思考时官方会忽略它）
- model_call_limit   一轮生成内最多几次模型调用（工具循环硬保险，默认 100；超出强制收尾）
- llm_provider       模型服务商：deepseek（官方）/ opencode（OpenCode Go）
- llm_api_key_deepseek / llm_api_key_opencode   各服务商的 API Key（切换服务商互不覆盖）
- llm_model_deepseek / llm_model_opencode       各服务商选用的模型（空 = 用预设默认）
- llm_base_url_deepseek / llm_base_url_opencode 端点覆盖（留空 = 用预设端点）
- llm_api_key        旧字段：DeepSeek Key 的历史兜底（读取兼容，UI 已改为分服务商）
- white_enabled      是否启用 White 子代理（后台独立执行任务）
- white_max_steps    White 单轮任务最多模型调用次数
- white_timeout_minutes  White 单轮任务超时（分钟）
- white_context_tokens   White 上下文预算（超过后摘要 + 滑动窗口）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_logger = logging.getLogger("planner.settings")

DEFAULT_SETTINGS: dict = {
    "press_ms": 200,
    "compress_keep": 20,
    "compress_token_budget": 100000,
    "compress_chunk_tokens": 30000,
    "compact_threshold": 8,
    "compact_factor": 4,
    "node_token_budget": 1000,
    "workspace_root": "",
    "workspace_cwd": "",
    "workspace_recent": [],
    "permission_mode": "ask",
    "approval_timeout_seconds": 120,
    "code_read_outside_workspace": False,
    "thinking_mode": "adaptive",
    "reasoning_effort": "high",
    "model_call_limit": 100,
    "llm_provider": "deepseek",
    "llm_api_key": "",                  # 旧字段：DeepSeek Key 的历史兜底
    "llm_api_key_deepseek": "",
    "llm_api_key_opencode": "",
    "llm_model_deepseek": "deepseek-flash",
    "llm_model_opencode": "deepseek-v4.1-flash",
    "llm_base_url_deepseek": "",
    "llm_base_url_opencode": "",
    "llm_session_id": "",               # OpenCode Go 会话头（首次使用时生成并持久化）
    "tts_enabled": True,
    "tts_engine": "",          # 留空 = 跟随环境变量 PLANNER_TTS_ENGINE；local / mimo / cloud
    "tts_voice": "zf_001",
    # 子代理 White（后台、无长期记忆；只在主工作区内读写、不能执行命令）
    "white_enabled": True,
    "white_max_steps": 30,
    "white_timeout_minutes": 10,
    "white_context_tokens": 60000,
}

# 数值范围护栏（前端输入 + POST 双重校验）
_LIMITS = {
    "press_ms": (50, 5000),
    "compress_keep": (5, 200),
    "compress_token_budget": (10000, 500000),
    "compress_chunk_tokens": (0, 100000),
    "compact_threshold": (3, 50),
    "compact_factor": (2, 10),
    "node_token_budget": (200, 4000),
    "approval_timeout_seconds": (60, 600),
    "model_call_limit": (10, 500),
    "white_max_steps": (5, 100),
    "white_timeout_minutes": (1, 120),
    "white_context_tokens": (10000, 120000),
}

# 布尔字段（前端可能传字符串，统一归一为 bool）
_BOOLS = {"tts_enabled", "code_read_outside_workspace", "white_enabled"}

# 列表字段（字符串数组；去重、截断上限）
_LISTS = {"workspace_recent": 8}

# 枚举字段：只接受这几个值。
# 官方 thinking.type 的三个变体都实测有效（2026-09-30）：disabled 时 reasoning token
# 真的是 0、同一道题快约 30%，而且此时 reasoning_effort 会被忽略。
_ENUMS = {
    "thinking_mode": ("adaptive", "enabled", "disabled"),
    "reasoning_effort": ("low", "high", "max"),
    "permission_mode": ("ask", "auto", "read_only"),
    "llm_provider": ("deepseek", "opencode"),
}


def settings_path(data_root: Path) -> Path:
    return Path(data_root) / "settings.json"


def load_settings(data_root: Path) -> dict:
    """读取设置（合并默认值，损坏文件静默回退默认）。

    旧字段迁移：`llm_api_key` / `llm_model`（历史单服务商配置）→ DeepSeek 分字段。
    """
    s = dict(DEFAULT_SETTINGS)
    raw: dict = {}
    try:
        p = settings_path(data_root)
        if p.exists():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
                for k in DEFAULT_SETTINGS:
                    if k in raw:
                        s[k] = raw[k]
    except Exception:
        _logger.warning("[settings] 读取失败，使用默认值")
        return s
    # 旧字段迁移（仅在分服务商字段缺省时）
    if "llm_api_key_deepseek" not in raw and str(raw.get("llm_api_key") or "").strip():
        s["llm_api_key_deepseek"] = str(raw["llm_api_key"]).strip()
    if "llm_model_deepseek" not in raw and str(raw.get("llm_model") or "").strip():
        s["llm_model_deepseek"] = str(raw["llm_model"]).strip()
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
        elif k in _ENUMS:
            allowed = _ENUMS[k]
            text = str(v or "").strip().lower()
            if text not in allowed:
                raise ValueError(f"{k} 只能是 {'/'.join(allowed)}")
            merged[k] = text
        elif k in _BOOLS:
            merged[k] = (v if isinstance(v, bool)
                         else str(v or "").strip().lower() in ("1", "true", "yes", "on"))
        elif k in _LISTS:
            # 列表字段（如 workspace_recent）：只接受字符串数组；去重保序、截断上限
            if isinstance(v, list):
                seen: list[str] = []
                for item in v:
                    s = str(item or "").strip()
                    if s and s not in seen:
                        seen.append(s)
                merged[k] = seen[: _LISTS[k]]
            # 非法类型：保持原值不动
        else:
            # 字符串化（空值归一为 ""）
            merged[k] = str(v or "")
    # 交叉约束：factor < threshold（叶子压缩只看 token 预算，keep 无配对约束）
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
