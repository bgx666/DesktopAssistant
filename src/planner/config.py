r"""planner 后端环境配置。

环境变量：
- PLANNER_PORT        监听端口，默认 18771
- PLANNER_MOCK_LLM    =1 时使用脚本化假 LLM（不调真实 API），供联调/测试
- PLANNER_DATA_ROOT   运行时数据根目录，默认项目根/planner/data
- LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 等：LLM 配置（默认 DeepSeek），
  从项目根 .env 或共享 .env（XIAOB_SHARED_ENV 指定路径）加载
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent          # src/planner/
PROJECT_ROOT = PACKAGE_ROOT.parent.parent               # 项目根
PROMPTS_DIR = PACKAGE_ROOT / "prompts"


def _load_env_files() -> None:
    """加载 .env：项目根 .env 优先，其次共享 .env（XIAOB_SHARED_ENV 指定路径）。"""
    local_env = PROJECT_ROOT / ".env"
    if local_env.exists():
        load_dotenv(local_env, override=False)
    shared = os.environ.get("XIAOB_SHARED_ENV")
    if shared:
        shared_env = Path(shared)
        if shared_env.exists():
            load_dotenv(shared_env, override=False)


_load_env_files()

PLANNER_PORT: int = int(os.getenv("PLANNER_PORT", "18771"))
PLANNER_MOCK_LLM: bool = os.getenv("PLANNER_MOCK_LLM", "").strip().lower() in ("1", "true", "yes", "on")

# 视觉模型默认值：planner 默认 deepseek-v4-flash-vision-exp（多模态，文本能力与 flash 持平）。
# 解析链（llm.py::resolve_model_name）：settings.llm_model > PLANNER_LLM_MODEL > 此默认值。
# 注意：共享 .env 的 LLM_MODEL 是 xiaob 游戏的配置，不再影响 planner。
PLANNER_DEFAULT_MODEL: str = "deepseek-v4-flash-vision-exp"

# 心跳护栏：LLM 自主决定心跳间隔，clamp 到 [PLANNER_HEARTBEAT_MIN, MAX] 分钟
# 心跳是分钟级定时任务（一人一句，不再秒级短心跳）
PLANNER_HEARTBEAT_MIN_MINUTES: float = float(os.getenv("PLANNER_HEARTBEAT_MIN_MINUTES", "10"))
PLANNER_HEARTBEAT_MAX_MINUTES: int = int(os.getenv("PLANNER_HEARTBEAT_MAX_MINUTES", "720"))

# 免打扰默认窗口（24h 制闭区间 [start, end)，跨天处理）
PLANNER_DND_START_HOUR: int = int(os.getenv("PLANNER_DND_START_HOUR", "22"))
PLANNER_DND_END_HOUR: int = int(os.getenv("PLANNER_DND_END_HOUR", "8"))

# 定时触发点（本地时间，UTC+8）
PLANNER_MORNING_HOUR: int = int(os.getenv("PLANNER_MORNING_HOUR", "8"))
PLANNER_EVENING_HOUR: int = int(os.getenv("PLANNER_EVENING_HOUR", "21"))

# 语音合成（本地 Kokoro-82M-zh 默认；PLANNER_TTS_ENGINE=cloud 时用 DashScope；
# PLANNER_TTS_ENGINE=mimo 时用小米 MiMo TTS，OpenAI 兼容接口）
PLANNER_TTS_ENGINE: str = os.getenv("PLANNER_TTS_ENGINE", "local").strip().lower()
PLANNER_TTS_API_KEY: str = os.getenv(
    "PLANNER_TTS_API_KEY", os.getenv("DASHSCOPE_API_KEY", "")).strip()
PLANNER_TTS_MODEL: str = os.getenv("PLANNER_TTS_MODEL", "qwen-audio-3.0-tts-flash")
PLANNER_TTS_VOICE: str = os.getenv("PLANNER_TTS_VOICE", "longanhuan_v3.6")

# 小米 MiMo TTS（OpenAI 兼容 /v1/chat/completions，TTS 系列当前限时免费）
PLANNER_MIMO_API_KEY: str = os.getenv(
    "PLANNER_MIMO_API_KEY", os.getenv("PLANNER_TTS_API_KEY", "")).strip()
PLANNER_MIMO_BASE_URL: str = os.getenv(
    "PLANNER_MIMO_BASE_URL", "https://api.xiaomimimo.com/v1").strip()
PLANNER_MIMO_MODEL: str = os.getenv("PLANNER_MIMO_MODEL", "mimo-v2.5-tts").strip()
PLANNER_MIMO_VOICE: str = os.getenv("PLANNER_MIMO_VOICE", "mimo_default").strip()

# LLM 兜底心跳（LLM 没调 heartbeat 时）
PLANNER_FALLBACK_MINUTES: int = int(os.getenv("PLANNER_FALLBACK_MINUTES", "60"))
# 自主学习保底间隔（模型自主学习后忘记设置心跳时兜底）
PLANNER_LEARNING_HEARTBEAT_MINUTES: int = int(
    os.getenv("PLANNER_LEARNING_HEARTBEAT_MINUTES", "30"))


def data_root() -> Path:
    """解析运行时数据根目录（默认项目根/data）。"""
    raw = os.getenv("PLANNER_DATA_ROOT")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    return (PROJECT_ROOT / "data").resolve()
