"""语音合成（TTS）测试：清洗、启用判定、合成落盘、audio 事件（mock，不调真实 API）。"""

from planner.session import PlannerSession


def test_clean_speech_text():
    from planner.tts import clean_speech_text
    assert clean_speech_text("") == ""
    assert clean_speech_text("**你好**，世界！") == "你好，世界！"
    assert clean_speech_text("看 [这个链接](http://x.com) 吧") == "看 这个链接 吧"
    assert clean_speech_text("`代码` 和 # 标题") == "代码 和 标题"
    assert clean_speech_text("第一行\n\n第二行") == "第一行。第二行"
    assert len(clean_speech_text("长" * 5000)) <= 2000


def test_disabled_without_key(tmp_path):
    from planner.tts import TtsClient
    c = TtsClient("", "m", "v", tmp_path)
    assert not c.enabled
    assert c.synthesize("你好") is None
    got = []
    c.synthesize_async("你好", lambda url: got.append(url))
    assert got == [None]


def test_synthesize_saves_audio(monkeypatch, tmp_path):
    import planner.tts as tts_mod

    calls = {}

    class _FakeSyn:
        def __init__(self, **kw):
            calls["model"] = kw.get("model")
            calls["voice"] = kw.get("voice")

        def call(self, text):
            calls["text"] = text
            return b"fake-mp3-bytes"

    fake_dashscope = type("FakeDashscope", (), {})()
    monkeypatch.setattr(tts_mod, "dashscope", fake_dashscope)
    monkeypatch.setattr(tts_mod, "_HAS_DASHSCOPE", True)
    monkeypatch.setattr(tts_mod, "SpeechSynthesizer", _FakeSyn)

    c = tts_mod.TtsClient("sk-test", "my-model", "my-voice", tmp_path)
    assert c.enabled
    url = c.synthesize("**你好**呀")
    assert url and url.startswith("/tts/") and url.endswith(".mp3")
    name = url.rsplit("/", 1)[1]
    assert len(name) == len("a" * 32) + 4 and name[:32].isalnum() and name[32:] == ".mp3"
    assert (tmp_path / "tts" / name).read_bytes() == b"fake-mp3-bytes"
    assert calls == {"model": "my-model", "voice": "my-voice", "text": "你好呀"}


def test_audio_event_after_generation(data_root):
    """完整文本收束 → _maybe_speak → audio 事件（气泡朗读入口）。"""
    s = PlannerSession(data_root, mock=True)
    try:
        class _FakeTts:
            enabled = True

            def __init__(self):
                self.texts = []

            def synthesize_async(self, text, on_done):
                self.texts.append(text)
                on_done("/tts/fake.mp3")

        fake = _FakeTts()
        s.tts = fake
        s._receive("你好呀", trigger=True)
        s.pending_response = False
        s._generate_response("player")
        events = s.drain_events()
        assert fake.texts, "应触发合成"
        assert any(e["type"] == "audio" and e["url"] == "/tts/fake.mp3" for e in events)
        # 文本事件仍然正常（toast 用）
        assert any(e["type"] == "text" for e in events)
    finally:
        s.close()


def test_audio_event_skipped_when_tts_disabled(data_root):
    """TTS 未启用（无 key）→ 不触发合成、无 audio 事件。"""
    s = PlannerSession(data_root, mock=True)
    try:
        if s.tts.enabled:
            return   # 测试机配了 key 时跳过断言（正常情况下未配置）
        s._receive("你好呀", trigger=True)
        s.pending_response = False
        s._generate_response("player")
        events = s.drain_events()
        assert not any(e["type"] == "audio" for e in events)
    finally:
        s.close()
