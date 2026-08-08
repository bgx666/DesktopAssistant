"""设置（settings）测试：读写、校验、动态压缩参数、LLM 配置重建、端点。"""

import json

import pytest

import planner.settings as settings_mod


def test_defaults_and_roundtrip(data_root):
    s = settings_mod.load_settings(data_root)
    assert s["press_ms"] == 200
    assert s["compress_trigger"] == 60
    merged = settings_mod.save_settings(data_root, {"press_ms": 350, "compress_trigger": 80})
    assert merged["press_ms"] == 350
    assert settings_mod.load_settings(data_root)["compress_trigger"] == 80


def test_validation(data_root):
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"press_ms": 10})          # 低于下限
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"compress_keep": 80, "compress_trigger": 60})  # keep >= trigger
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"compact_factor": 8, "compact_threshold": 4})  # factor >= threshold
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"press_ms": "abc"})


def test_unknown_keys_ignored(data_root):
    merged = settings_mod.save_settings(data_root, {"hack": 1, "press_ms": 300})
    assert "hack" not in merged
    assert merged["press_ms"] == 300


def test_corrupted_file_falls_back(data_root, monkeypatch):
    p = settings_mod.settings_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{bad json", encoding="utf-8")
    s = settings_mod.load_settings(data_root)
    assert s["compress_trigger"] == 60


def test_session_update_settings(data_root):
    from planner.session import PlannerSession
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"press_ms": 400, "compress_trigger": 80})
        assert s.settings["press_ms"] == 400
        assert s.settings["compress_trigger"] == 80
        with pytest.raises(ValueError):
            s.update_settings({"press_ms": 1})
    finally:
        s.close()


def test_compress_params_dynamic(data_root, monkeypatch):
    """压缩参数从 session.settings 动态读取（应用即生效）。"""
    from langchain_core.messages import HumanMessage
    from planner.middleware import SummarizationMiddleware, MemoryNodeOutput
    from planner.session import PlannerSession

    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_trigger": 30, "compress_keep": 10})
        comp = SummarizationMiddleware(s)
        monkeypatch.setattr(comp, "_call_compress", lambda ctx, ins: MemoryNodeOutput(
            summary="摘要", profile={"preferences": [], "personality": [],
                                     "habits": [], "goals": []}, future_notes=[]))
        # 30 条触发（旧逻辑 60 条不触发）
        msgs = [HumanMessage(content=f"m{i}", metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(30)]
        removes, adds = comp.compress_snapshot(msgs)
        assert removes and adds, "30 条时应触发压缩（设置 30 触发）"
        # batch = 最早 30-10=20 条
        assert len(removes) == 20
    finally:
        s.close()


def test_compact_params_dynamic(data_root, monkeypatch):
    """层级合并参数从 settings 读取：阈值 5、合并 2。"""
    from planner.middleware import SummarizationMiddleware, MemoryNodeOutput
    from planner.session import PlannerSession

    s = PlannerSession(data_root, mock=True)
    try:
        tree = s.get_memory_tree()
        # 直接造 5 个叶子
        for i in range(5):
            tree.add_leaf(f"摘要{i}", (i * 10, i * 10 + 9), None)
        comp = SummarizationMiddleware(s)
        monkeypatch.setattr(comp, "_call_compress", lambda ctx, ins: MemoryNodeOutput(
            summary="父摘要", profile={"preferences": [], "personality": [],
                                      "habits": [], "goals": []}, future_notes=[]))
        # 默认阈值 8 → 5 个叶子不合并
        removes, adds = comp.compress_snapshot([])
        assert not adds
        # 设置阈值 5、合并 2 → 触发一次（5 → 4+1父）
        s.update_settings({"compact_threshold": 5, "compact_factor": 2})
        removes, adds = comp.compress_snapshot([])
        assert adds, "阈值 5 时应合并"
        nodes0 = tree.get_nodes_at_level(0)
        nodes1 = tree.get_nodes_at_level(1)
        assert len(nodes1) == 1
        assert len(nodes0) == 3
    finally:
        s.close()
