r"""planner 后端环境配置。

环境变量：
- PLANNER_PORT        监听端口，默认 18771
- PLANNER_MOCK_LLM    =1 时使用脚本化假 LLM（不调真实 API），供联调/测试
- PLANNER_DATA_ROOT   运行时数据根目录，默认项目根/planner/data
- LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 等：LLM 配置（默认 DeepSeek），
  从本地 .env 或 D:\xiaob\.env 加载（只读配置，不 import xiaob）
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent          # src/planner/
PROJECT_ROOT = PACKAGE_ROOT.parent.parent               # 项目根（D:\xiaob\planner\）
PROMPTS_DIR = PACKAGE_ROOT / "prompts"

_DEFAULT_XIAOB_ENV = Path(r"D:\xiaob\.env")


def _load_env_files() -> None:
    """加载 .env：项目本地优先，其次 D:\\xiaob\\.env（LLM_API_KEY 等复用）。"""
    local_env = PROJECT_ROOT / ".env"
    if local_env.exists():
        load_dotenv(local_env, override=False)
    if _DEFAULT_XIAOB_ENV.exists():
        load_dotenv(_DEFAULT_XIAOB_ENV, override=False)


_load_env_files()

PLANNER_PORT: int = int(os.getenv("PLANNER_PORT", "18771"))
PLANNER_MOCK_LLM: bool = os.getenv("PLANNER_MOCK_LLM", "").strip().lower() in ("1", "true", "yes", "on")

# 心跳护栏：LLM 自主决定心跳间隔，clamp 到 [PLANNER_HEARTBEAT_MIN, MAX] 分钟
PLANNER_HEARTBEAT_MIN_MINUTES: int = int(os.getenv("PLANNER_HEARTBEAT_MIN_MINUTES", "10"))
PLANNER_HEARTBEAT_MAX_MINUTES: int = int(os.getenv("PLANNER_HEARTBEAT_MAX_MINUTES", "720"))

# 免打扰默认窗口（24h 制闭区间 [start, end)，跨天处理）
PLANNER_DND_START_HOUR: int = int(os.getenv("PLANNER_DND_START_HOUR", "22"))
PLANNER_DND_END_HOUR: int = int(os.getenv("PLANNER_DND_END_HOUR", "8"))

# 定时触发点（本地时间，UTC+8）
PLANNER_MORNING_HOUR: int = int(os.getenv("PLANNER_MORNING_HOUR", "8"))
PLANNER_EVENING_HOUR: int = int(os.getenv("PLANNER_EVENING_HOUR", "21"))

# LLM 兜底心跳（LLM 没调 heartbeat 时）
PLANNER_FALLBACK_MINUTES: int = int(os.getenv("PLANNER_FALLBACK_MINUTES", "60"))


def data_root() -> Path:
    """解析运行时数据根目录（默认项目根/data）。"""
    raw = os.getenv("PLANNER_DATA_ROOT")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    return (PROJECT_ROOT / "data").resolve()
