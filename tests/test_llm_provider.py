"""模型服务商（DeepSeek 官方 / OpenCode Go）：配置解析、分服务商 Key/模型、迁移、端点。"""

import json

import pytest

from planner import settings as settings_mod
from planner.llm import PROVIDER_PRESETS, resolve_llm_config
from planner.session import PlannerSession


def test_provider_presets_shape():
    assert set(PROVIDER_PRESETS) == {"deepseek", "opencode"}
    assert PROVIDER_PRESETS["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert PROVIDER_PRESETS["opencode"]["base_url"] == "https://opencode.ai/inference/go/openai/v1"
    assert PROVIDER_PRESETS["opencode"]["models_url"] == "https://opencode.ai/inference/go/v1/models"
    assert "deepseek-flash" in PROVIDER_PRESETS["deepseek"]["models"]
    assert "deepseek-v4.1-flash" in PROVIDER_PRESETS["opencode"]["models"]
    # OpenCode Go 只收录 chat/completions 兼容的模型（responses/messages 端点模型不进来）
    assert all("-luna" not in m and not m.startswith("claude") and not m.startswith("grok")
               and not m.startswith("qwen") and not m.startswith("minimax")
               and not m.startswith("gemini")
               for m in PROVIDER_PRESETS["opencode"]["models"])


def test_provider_headers():
    """OpenCode Go 需要会话头 + 自定义 UA；DeepSeek 不加任何头。"""
    from planner.llm import provider_headers
    assert provider_headers("deepseek", "sid") == {}
    h = provider_headers("opencode", "sid-1")
    assert h.get("x-opencode-session") == "sid-1"
    assert "xiaozhu" in str(h.get("User-Agent", "")).lower()
    h2 = provider_headers("opencode", "")
    assert "x-opencode-session" not in h2 and h2.get("User-Agent")


def test_build_chat_model_passes_opencode_headers(monkeypatch):
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import planner.llm as llm_module
    monkeypatch.setattr(llm_module, "ChatOpenAI", FakeChatOpenAI)
    llm_module.build_chat_model(api_key="k", base_url="https://opencode.ai/inference/go/openai/v1",
                                model_name="glm-5.3", provider="opencode", session_id="sid9")
    headers = captured.get("default_headers") or {}
    assert headers.get("x-opencode-session") == "sid9"
    assert "xiaozhu" in str(headers.get("User-Agent", "")).lower()


def test_opencode_request_really_carries_session_header(data_root, monkeypatch):
    """真发一次请求（打本地假端点）：OpenCode Go 必须带 x-opencode-session + 自定义 UA + Bearer。

    回归：缺会话头时 OpenCode Go 返回 400 MissingSessionID（用户实际踩到过）。
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from langchain_core.messages import HumanMessage

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen["path"] = self.path
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            self.rfile.read(length)
            body = _json.dumps({
                "id": "1", "object": "chat.completion", "created": 0, "model": "glm-5.3",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):   # 静默
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        s = PlannerSession(data_root, mock=True)
        try:
            monkeypatch.setattr(s, "mock", False)
            s.settings.update({
                "llm_provider": "opencode",
                "llm_api_key_opencode": "sk-test",
                "llm_model_opencode": "glm-5.3",
                "llm_base_url_opencode": f"http://127.0.0.1:{port}/v1",
                "llm_session_id": "sess-test-1",
            })
            s._llm = None
            model = s._get_llm()
            model.disable_streaming = True   # 假端点返回整段 JSON（非流式）便于断言
            msg = model.invoke([HumanMessage(content="hi")])
            assert str(getattr(msg, "content", "")) == "hi"
        finally:
            s.close()
    finally:
        httpd.shutdown()
    assert seen.get("path") == "/v1/chat/completions"
    h = seen.get("headers") or {}
    assert h.get("x-opencode-session") == "sess-test-1"
    assert "xiaozhu" in str(h.get("user-agent", "")).lower()
    assert h.get("authorization") == "Bearer sk-test"


def test_generation_without_key_pushes_error_event(data_root, monkeypatch):
    """没配 Key 直接发消息 → 必须推 error 事件（前端红色气泡），不能静默失败。"""
    s = PlannerSession(data_root, mock=True)
    try:
        monkeypatch.setattr(s, "mock", False)
        s.settings.update({"llm_provider": "deepseek", "llm_api_key_deepseek": "",
                           "llm_api_key": ""})
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        s._run_agent()
        errs = [e for e in s.drain_events() if e.get("type") == "error"]
        assert errs, "应推送 error 事件"
        assert "API Key" in errs[0]["text"] and errs[0]["hint"]
    finally:
        s.close()


def test_resolve_llm_config_defaults(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
    cfg = resolve_llm_config({})
    assert cfg["provider"] == "deepseek"
    assert cfg["base_url"] == "https://api.deepseek.com"
    assert cfg["model"] == "deepseek-flash"
    assert cfg["api_key"] == ""


def test_resolve_llm_config_opencode(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
    cfg = resolve_llm_config({"llm_provider": "opencode",
                              "llm_api_key_opencode": "sk-zen",
                              "llm_model_opencode": "glm-5.3"})
    assert cfg["base_url"] == "https://opencode.ai/inference/go/openai/v1"
    assert cfg["api_key"] == "sk-zen" and cfg["model"] == "glm-5.3"
    # DeepSeek 的 key/模型不影响 OpenCode；缺省用 OpenCode 预设默认
    cfg2 = resolve_llm_config({"llm_provider": "opencode",
                               "llm_api_key_deepseek": "sk-ds",
                               "llm_model_deepseek": "deepseek-flash"})
    assert cfg2["api_key"] == ""
    assert cfg2["model"] == "deepseek-v4.1-flash"


def test_resolve_llm_config_legacy_fallback(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("PLANNER_LLM_MODEL", raising=False)
    # 旧字段只在 DeepSeek 下兜底；分服务商字段优先
    cfg = resolve_llm_config({"llm_api_key": "sk-old", "llm_model": "deepseek-v4-flash"})
    assert cfg["api_key"] == "sk-old" and cfg["model"] == "deepseek-v4-flash"
    cfg2 = resolve_llm_config({"llm_api_key": "sk-old", "llm_api_key_deepseek": "sk-new"})
    assert cfg2["api_key"] == "sk-new"
    # OpenCode 不吃旧字段
    cfg3 = resolve_llm_config({"llm_provider": "opencode", "llm_api_key": "sk-old"})
    assert cfg3["api_key"] == ""


def test_resolve_llm_config_base_url_override(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = resolve_llm_config({"llm_provider": "opencode",
                              "llm_base_url_opencode": "http://127.0.0.1:9999/v1"})
    assert cfg["base_url"] == "http://127.0.0.1:9999/v1"


def test_settings_provider_roundtrip(data_root):
    merged = settings_mod.save_settings(data_root, {
        "llm_provider": "opencode",
        "llm_api_key_opencode": "sk-zen",
        "llm_model_opencode": "kimi-k3",
    })
    assert merged["llm_provider"] == "opencode"
    loaded = settings_mod.load_settings(data_root)
    assert loaded["llm_api_key_opencode"] == "sk-zen"
    assert loaded["llm_model_opencode"] == "kimi-k3"
    with pytest.raises(ValueError):
        settings_mod.save_settings(data_root, {"llm_provider": "anthropic"})


def test_legacy_settings_migration(data_root):
    """老 settings.json（llm_api_key / llm_model）→ 迁移到 DeepSeek 分字段。"""
    (data_root / "settings.json").write_text(
        json.dumps({"llm_api_key": "sk-old", "llm_model": "deepseek-v4-pro"},
                   ensure_ascii=False), encoding="utf-8")
    loaded = settings_mod.load_settings(data_root)
    assert loaded["llm_api_key_deepseek"] == "sk-old"
    assert loaded["llm_model_deepseek"] == "deepseek-v4-pro"


def test_update_settings_provider_rebuilds_model(data_root, monkeypatch):
    s = PlannerSession(data_root, mock=True)
    try:
        monkeypatch.setattr(s, "mock", False)
        s._llm = object()           # 占位：模拟已构建的模型实例
        s._summary_model = object()
        s.update_settings({"llm_provider": "opencode"})
        assert s._llm is None and s._summary_model is None
        cfg = resolve_llm_config(s.settings)
        assert cfg["provider"] == "opencode"
        assert cfg["model"] == "deepseek-v4.1-flash"
    finally:
        s.close()
