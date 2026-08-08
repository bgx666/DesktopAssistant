"""语音合成（TTS）测试：清洗、引擎选择、本地引擎 mock、audio 事件（mock，不调真实 API）。"""

from planner.session import PlannerSession


def test_clean_speech_text():
    from planner.tts import clean_speech_text
    assert clean_speech_text("") == ""
    assert clean_speech_text("**你好**，世界！") == "你好，世界！"
    assert clean_speech_text("看 [这个链接](http://x.com) 吧") == "看 这个链接 吧"
    assert clean_speech_text("`代码` 和 # 标题") == "代码 和 标题"
    assert clean_speech_text("第一行\n\n第二行") == "第一行。第二行"
    assert len(clean_speech_text("长" * 5000)) <= 2000


def test_cloud_disabled_without_key(tmp_path):
    from planner.tts import TtsClient
    c = TtsClient(tmp_path, engine="cloud", api_key="", model="m", voice="v")
    assert not c.enabled
    assert c.synthesize("你好") is None
    got = []
    c.synthesize_async("你好", lambda url: got.append(url))
    assert got == [None]


def test_cloud_synthesize_saves_audio(monkeypatch, tmp_path):
    import planner.tts as tts_mod

    calls = {}

    class _FakeSyn:
        def __init__(self, **kw):
            calls["model"] = kw.get("model")
            calls["voice"] = kw.get("voice")

        def call(self, text):
            calls["text"] = text
            return b"fake-mp3-bytes"

    monkeypatch.setattr(tts_mod, "dashscope", type("FakeDashscope", (), {})())
    monkeypatch.setattr(tts_mod, "_HAS_DASHSCOPE", True)
    monkeypatch.setattr(tts_mod, "SpeechSynthesizer", _FakeSyn)

    c = tts_mod.TtsClient(tmp_path, engine="cloud", api_key="sk-test",
                          model="my-model", voice="my-voice")
    assert c.enabled
    url = c.synthesize("**你好**呀")
    assert url and url.startswith("/tts/") and url.endswith(".mp3")
    name = url.rsplit("/", 1)[1]
    assert name[:32].isalnum() and name[32:] == ".mp3"
    assert (tmp_path / "tts" / name).read_bytes() == b"fake-mp3-bytes"
    assert calls == {"model": "my-model", "voice": "my-voice", "text": "你好呀"}


def test_local_engine_enabled_by_default(monkeypatch, tmp_path):
    """本地引擎默认启用（无需 key）；合成失败静默返回 None。"""
    from planner.tts import TtsClient
    c = TtsClient(tmp_path, engine="local", voice="zf_001")
    assert c.enabled
    monkeypatch.setattr(c._local, "synthesize", lambda text: None)
    assert c.synthesize("你好") is None


def test_local_phoneme_tokens(monkeypatch, tmp_path):
    """音素转换管线：箭头→数字、字符映射、小写、vocab 查询。"""
    import json
    import numpy as np

    import planner.tts as tts_mod

    # 假 vocab：包含常见字符（真实模型 vocab 子集）
    vocab = {"n": 0, "i": 1, "3": 2, "x": 3, "a": 4, "u": 5, " ": 6,
             "h": 7, "o": 8, "χ": 9, "ʐ": 10, ",": 11, "e": 12, "l": 13}
    model_dir = tmp_path / "model"
    (model_dir / "onnx").mkdir(parents=True)
    (model_dir / "voices").mkdir()
    (model_dir / "tokenizer.json").write_text(
        json.dumps({"model": {"vocab": vocab}}), encoding="utf-8")
    (model_dir / "onnx" / "model_int8.onnx").write_bytes(b"fake")
    (model_dir / "voices" / "zf_001.bin").write_bytes(np.zeros(510 * 256, np.float32).tobytes())

    k = tts_mod._KokoroLocal(model_dir, voice="zf_001")

    # 不加载 onnx（只测音素→tokens）
    k._vocab = vocab
    k._sess = object()
    k._g2p = lambda text: "ni\u2193xau\u2193"      # ni↓xau↓（↓=3声）
    tokens = k._phonemes_to_tokens("你好")
    assert tokens == [vocab["n"], vocab["i"], vocab["3"], vocab["x"], vocab["a"], vocab["u"], vocab["3"]]


def test_maybe_speak_calls_tts(data_root):
    """_maybe_speak → 调 tts.synthesize_async + audio 事件（非 mock 模式）。"""
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
        s.mock = False
        s._maybe_speak("你好呀")
        assert fake.texts == ["你好呀"], "应触发合成"
        events = s.drain_events()
        assert any(e["type"] == "audio" and e["url"] == "/tts/fake.mp3" for e in events)
        # mock 模式跳过合成（性能：测试/演示不朗读）
        s.mock = True
        s._maybe_speak("再见了")
        assert fake.texts == ["你好呀"], "mock 模式不应合成"
    finally:
        s.close()


def test_audio_event_skipped_when_tts_disabled(data_root, monkeypatch):
    """云引擎且无 key → 不触发合成、无 audio 事件。"""
    import planner.tts as tts_mod
    monkeypatch.setattr(tts_mod, "_HAS_DASHSCOPE", False)
    from planner import config as _config
    monkeypatch.setattr(_config, "PLANNER_TTS_ENGINE", "cloud")
    monkeypatch.setattr(_config, "PLANNER_TTS_API_KEY", "")
    s = PlannerSession(data_root, mock=True)
    try:
        assert not s.tts.enabled
        s._receive("你好呀", trigger=True)
        s.pending_response = False
        s._generate_response("player")
        events = s.drain_events()
        assert not any(e["type"] == "audio" for e in events)
    finally:
        s.close()
