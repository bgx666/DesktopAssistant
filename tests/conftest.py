"""pytest 共享配置：所有测试 mock LLM、tmp_path 隔离文件，不写真实 data/。"""

import os
import sys
from pathlib import Path

# 必须在 import planner 之前设置（config 模块级读环境变量）
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("PLANNER_MOCK_LLM", "1")

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402


@pytest.fixture
def data_root(tmp_path):
    """每个测试独立的临时数据根目录。"""
    return tmp_path
