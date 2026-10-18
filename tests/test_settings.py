"""设置（settings）测试：读写、校验、动态压缩参数、LLM 配置重建、端点。"""

import json

import pytest

import planner.settings as settings_mod


def test_defaults_and_roundtrip(data_root):
    s = settings_mod.load_settings(data_root)
    assert s["press_ms"] == 200
    assert s["compress_token_budget"] == 100000
    assert s["model_call_limit"] == 100   # 干活模式：默认 100 次单轮调用上限
    merged = settings_mod.save_settings(data_root, {"press_ms": 350,
                                                    "compress_token_budget": 20000})
    assert merged["press_ms"] == 350
    assert settings_mod.load_settings(data_root)["compress_token_budget"] == 20000


def test_model_call_limit_validation(data_root):
    """单轮调用上限：10~500，越界/非法值报错；正常值可存。"""
    merged = settings_mod.save_settings(data_root, {"model_call_limit": 300})
    assert merged["model_call_limit"] == 300
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"model_call_limit": 5})     # 低于下限
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"model_call_limit": 600})   # 高于上限
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"model_call_limit": "abc"})


def test_model_call_limit_used_by_agent_and_rebuilds(data_root, monkeypatch):
    """agent 构建读 model_call_limit；改设置重建 agent。"""
    import planner.agent as agent_mod
    from planner.session import PlannerSession

    captured = {}

    def _fake_create_agent(**kwargs):
        captured["middleware"] = kwargs.get("middleware")
        return object()

    monkeypatch.setattr(agent_mod, "create_agent", _fake_create_agent)
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"model_call_limit": 200})
        _ = s._agent   # 触发构建（property）
        mws = captured["middleware"]
        limits = [m for m in mws if type(m).__name__ == "ModelCallLimitMiddleware"]
        assert limits and int(limits[0].run_limit) == 200
        # 改上限 → agent 重建（新值生效）
        s._agent_obj = "sentinel"
        s.update_settings({"model_call_limit": 50})
        assert s._agent_obj is None
    finally:
        s.close()


def test_limit_message_neutralized(data_root):
    """超限注入的英文 AI 消息 → 写回 buffer 前替换成中文提示。"""
    from langchain_core.messages import AIMessage

    from planner.session import PlannerSession

    s = PlannerSession(data_root, mock=True)
    try:
        m = AIMessage(content="Model call limits exceeded: run limit (100/100)")
        assert s._neutralize_limit_message(m) is True
        assert "单轮调用上限" in str(m.content)
        assert "Model call limits" not in str(m.content)
        normal = AIMessage(content="普通回复")
        assert s._neutralize_limit_message(normal) is False
        assert normal.content == "普通回复"
    finally:
        s.close()


def test_validation(data_root):
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"press_ms": 10})          # 低于下限
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"compress_token_budget": 1000})  # 低于下限
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"compact_factor": 8, "compact_threshold": 4})  # factor >= threshold
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"press_ms": "abc"})


def test_unknown_keys_ignored(data_root):
    merged = settings_mod.save_settings(data_root, {"hack": 1, "press_ms": 300})
    assert "hack" not in merged
    assert merged["press_ms"] == 300


def test_tts_enabled_bool_roundtrip(data_root):
    """布尔开关保真：false 不能存成 ""（曾致关闭后重开仍显示启用）。"""
    merged = settings_mod.save_settings(data_root, {"tts_enabled": False})
    assert merged["tts_enabled"] is False
    assert settings_mod.load_settings(data_root)["tts_enabled"] is False
    merged = settings_mod.save_settings(data_root, {"tts_enabled": True})
    assert merged["tts_enabled"] is True


def test_corrupted_file_falls_back(data_root, monkeypatch):
    p = settings_mod.settings_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{bad json", encoding="utf-8")
    s = settings_mod.load_settings(data_root)
    assert s["compress_token_budget"] == 100000


def test_session_update_settings(data_root):
    from planner.session import PlannerSession
    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"press_ms": 400, "compress_token_budget": 20000})
        assert s.settings["press_ms"] == 400
        assert s.settings["compress_token_budget"] == 20000
        with pytest.raises(ValueError):
            s.update_settings({"press_ms": 1})
    finally:
        s.close()


def test_tts_settings_applied_on_startup(data_root, monkeypatch):
    """重启场景：settings.json 里的 tts 配置（开关/音色）启动即生效——
    曾 bug：只在保存时生效，重启后回到默认（语音莫名开启、音色丢失）。

    显式把引擎钉成 local：音色名是**分引擎**的（zf_003 是本地 Kokoro 的音色），
    不钉的话，环境里 `PLANNER_TTS_ENGINE=mimo`（共享 .env 里给 release 配的）
    会让断言变成"在 MiMo 上应用一个 Kokoro 音色"——那不是这个测试要问的事。
    """
    import json
    from planner.session import PlannerSession
    p = settings_mod.settings_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tts_enabled": False, "tts_voice": "zf_003",
                             "tts_engine": "local"}),
                 encoding="utf-8")
    monkeypatch.delenv("PLANNER_TTS_ENGINE", raising=False)
    s = PlannerSession(data_root, mock=True)
    try:
        assert s.tts.engine == "local"
        assert s.tts._enabled is False, "启动应应用 tts_enabled=false"
        assert s.tts.voice == "zf_003", "启动应应用 tts_voice"
    finally:
        s.close()


def test_tts_voice_from_another_engine_is_not_applied(data_root, monkeypatch):
    """分引擎的音色名不许串台：MiMo 上不该应用一个 Kokoro 音色。

    这是 `_create_tts` 里 `voice in {v["id"] for v in tts.list_voices()}`
    那道守卫的用途——没有它，切换引擎后旧音色会被塞进去，
    合成时要么报错、要么退回默认而用户以为设置没生效。
    """
    import json
    from planner.session import PlannerSession
    p = settings_mod.settings_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tts_engine": "mimo", "tts_voice": "zf_003"}),
                 encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        assert s.tts.engine == "mimo"
        assert s.tts.voice != "zf_003", "别的引擎的音色名不该被应用"
        assert s.tts.voice in {v["id"] for v in s.tts.list_voices()}
    finally:
        s.close()


def test_compress_params_dynamic(data_root, monkeypatch):
    """压缩参数从 session.settings 动态读取（叶子压缩只看 token 预算）。"""
    from langchain_core.messages import HumanMessage
    from planner.middleware import SummarizationMiddleware, MemoryNodeOutput
    from planner.session import PlannerSession

    s = PlannerSession(data_root, mock=True)
    try:
        s.update_settings({"compress_token_budget": 10000, "compress_keep": 10})
        comp = SummarizationMiddleware(s)
        monkeypatch.setattr(comp, "_call_compress", lambda ctx, ins: MemoryNodeOutput(
            summary="摘要"))
        # 30 条 × ~300 字符 ≈ 10800 token ≥ 10000 预算 → 触发
        msgs = [HumanMessage(content=f"m{i}" + "x" * 298,
                             metadata={"ts": "2026-08-08 10:00:00"})
                for i in range(30)]
        removes, adds = comp.compress_snapshot(msgs)
        assert removes and adds, "token 超预算时应触发压缩"
        # batch = 最早 30-10=20 条
        assert len(removes) == 20
        # 预算调大 → 不再触发（应用即生效）
        s.update_settings({"compress_token_budget": 500000})
        removes2, adds2 = comp.compress_snapshot(msgs)
        assert not removes2 and not adds2, "预算调大后不应再触发"
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
            summary="父摘要"))
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
