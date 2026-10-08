"""思考开关与强度：设置读写 + 请求参数拼装。

为什么单独测这个：
- 这两项是用户唯一能直接调"聪明度 vs 速度"的旋钮，写错了体感差别很大
- LangChain 把 extra_body 交给底层 client 合并，实例上看不到，
  所以把拼装抽成了 `thinking_extra_body` 纯函数来测
"""
import json

import pytest

from planner.llm import REASONING_EFFORTS, THINKING_MODES, thinking_extra_body
from planner.settings import DEFAULT_SETTINGS, load_settings, save_settings


# ── 设置读写 ────────────────────────────────────────────────

def test_defaults_are_adaptive_and_high():
    """默认自适应 + 官方默认档 high。"""
    assert DEFAULT_SETTINGS["thinking_mode"] == "adaptive"
    assert DEFAULT_SETTINGS["reasoning_effort"] == "high"


def test_save_and_reload(tmp_path):
    save_settings(tmp_path, {"thinking_mode": "disabled", "reasoning_effort": "max"})
    again = load_settings(tmp_path)
    assert again["thinking_mode"] == "disabled"
    assert again["reasoning_effort"] == "max"


def test_rejects_unknown_enum(tmp_path):
    with pytest.raises(ValueError):
        save_settings(tmp_path, {"thinking_mode": "sometimes"})
    with pytest.raises(ValueError):
        save_settings(tmp_path, {"reasoning_effort": "insane"})


def test_accepts_all_documented_values(tmp_path):
    for mode in THINKING_MODES:
        assert save_settings(tmp_path, {"thinking_mode": mode})["thinking_mode"] == mode
    for effort in REASONING_EFFORTS:
        assert save_settings(tmp_path, {"reasoning_effort": effort})["reasoning_effort"] == effort


def test_old_settings_file_without_new_keys_still_loads(tmp_path):
    """老配置文件（没有这两个键）要能正常读，并补上默认值。"""
    (tmp_path / "settings.json").write_text(
        json.dumps({"press_ms": 300, "compress_trigger": 60}), encoding="utf-8"
    )
    loaded = load_settings(tmp_path)
    assert loaded["press_ms"] == 300
    assert loaded["thinking_mode"] == "adaptive"
    assert loaded["reasoning_effort"] == "high"


# ── 请求参数拼装 ────────────────────────────────────────────

def test_disabled_sends_no_effort():
    """关闭思考时**不发** reasoning_effort（官方会忽略它，发了等于骗自己）。"""
    extra = thinking_extra_body("deepseek-flash", "disabled", "max")
    assert extra == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in extra


def test_adaptive_and_enabled_keep_effort():
    assert thinking_extra_body("deepseek-flash", "adaptive", "low") == {
        "thinking": {"type": "adaptive"},
        "reasoning_effort": "low",
    }
    assert thinking_extra_body("deepseek-flash", "enabled", "max") == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


def test_defaults_when_unset():
    assert thinking_extra_body("deepseek-flash", None, None) == {
        "thinking": {"type": "adaptive"},
        "reasoning_effort": "high",
    }


def test_dirty_values_fall_back():
    """设置页传了脏值也不能发到线上。"""
    assert thinking_extra_body("deepseek-flash", "??", "??") == {
        "thinking": {"type": "adaptive"},
        "reasoning_effort": "high",
    }


def test_non_deepseek_models_get_nothing():
    """别的服务商不认这两个字段，发了会 400。"""
    assert thinking_extra_body("gpt-4o", "enabled", "max") == {}
    assert thinking_extra_body("", "enabled", "max") == {}
    assert thinking_extra_body("qwen-max", "adaptive", "high") == {}


def test_deepseek_v4_family_is_covered():
    """PC 的默认模型名与**官方现名**都要覆盖到。

    `deepseek-flash` 这一条是测试抓出来的真 bug：原来的前缀判断只认
    `deepseek-v4`，用官方名时设置页里的思考开关会静默失效。
    """
    for name in ("deepseek-v4-flash-vision-exp", "deepseek-v4", "deepseek-v4-pro"):
        assert thinking_extra_body(name, "disabled", None) == {"thinking": {"type": "disabled"}}
    for name in ("deepseek-flash", "DeepSeek-Flash", " deepseek-flash "):
        assert thinking_extra_body(name, "adaptive", "high") == {
            "thinking": {"type": "adaptive"},
            "reasoning_effort": "high",
        }


def test_build_chat_model_passes_extra_body(monkeypatch):
    """确认 build_chat_model 真的把拼出来的 extra_body 交给了 ChatOpenAI。"""
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import planner.llm as llm_module

    monkeypatch.setattr(llm_module, "ChatOpenAI", FakeChatOpenAI)
    llm_module.build_chat_model(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        model_name="deepseek-flash",
        thinking_mode="disabled",
        reasoning_effort="max",
    )
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["model"] == "deepseek-flash"


def test_build_chat_model_omits_extra_body_for_other_vendors(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import planner.llm as llm_module

    monkeypatch.setattr(llm_module, "ChatOpenAI", FakeChatOpenAI)
    llm_module.build_chat_model(api_key="sk-test", base_url="https://x", model_name="gpt-4o")
    assert "extra_body" not in captured
