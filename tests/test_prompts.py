"""提示词统一管理（prompts/ + prompt_loader）：注册表、渲染、注入标记。"""

import pytest

from planner import prompt_loader
from planner.session import PlannerSession


def test_prompt_registry_files_exist():
    """注册表里的每个逻辑名都能读到非空文件（漏文件/忘登记都会红）。"""
    assert prompt_loader.PROMPTS
    for name in prompt_loader.PROMPTS:
        assert prompt_loader.load(name).strip(), name


def test_system_prompt_has_memory_first_routing():
    """回忆类问题必须优先 explore_memory_tree（这是用户明确要求的路由规则）。"""
    text = prompt_loader.load("system")
    assert "第一选择是 `explore_memory_tree`" in text
    assert "grep" in text and "read_file" in text and "web_search" in text


def test_prompt_render_rejects_missing_placeholder():
    with pytest.raises(prompt_loader.PromptRenderError):
        prompt_loader.render("runtime/player_message", time="12:00")


def test_prompt_unknown_name():
    with pytest.raises(prompt_loader.PromptNotFound):
        prompt_loader.load("runtime/does_not_exist")


def test_internal_injection_metadata(data_root):
    """内部注入带 metadata.injected / kind；真实用户消息不带。"""
    s = PlannerSession(data_root, mock=True)
    try:
        m = s._receive("（系统：测试）", trigger=False, injected=True,
                       kind="calendar_due")
        assert m.metadata.get("injected") is True
        assert m.metadata.get("kind") == "calendar_due"
        m2 = s._receive("[12:00] 用户对你说：hi", trigger=False)
        assert not m2.metadata.get("injected")
    finally:
        s.close()
