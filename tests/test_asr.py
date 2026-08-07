"""语音输入（SenseVoiceSmall-onnx）测试：wav 解析、标签剥离、识别、端点（mock，不下载模型）。"""

import io
import struct
import wave

import pytest

import planner.asr as asr_mod


def _make_wav(samples_int16, sr=16000, nch=1, width=2):
    """构造 wav bytes（标准库 wave 写内存）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(width)
        wf.setframerate(sr)
        wf.writeframes(struct.pack(f"<{len(samples_int16)}h", *samples_int16))
    return buf.getvalue()


def _silence(n_sec=1.0, sr=16000, amp=100):
    n = int(n_sec * sr)
    return [amp] * n   # 直流信号（非零但无语音，识别不依赖内容）


def test_strip_tags():
    assert asr_mod.strip_tags("<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好，小助。") == "你好，小助。"
    assert asr_mod.strip_tags("  纯文本  ") == "纯文本"
    assert asr_mod.strip_tags("") == ""


def test_wav_to_float32_basic():
    raw = _make_wav(_silence(0.5))
    w = asr_mod.wav_to_float32(raw)
    assert w is not None and len(w) == 8000
    assert abs(w[0]) <= 1.0
    # 非法输入
    assert asr_mod.wav_to_float32(b"") is None
    assert asr_mod.wav_to_float32(b"not a wav") is None
    assert asr_mod.wav_to_float32(b"x" * (21 * 1024 * 1024)) is None


def test_wav_to_float32_resample_and_stereo():
    raw = _make_wav(_silence(0.5, sr=8000), sr=8000)          # 8k → 16k
    w = asr_mod.wav_to_float32(raw)
    assert w is not None and len(w) == 8000
    raw2 = _make_wav(_silence(1.0), nch=2)                     # 双声道：16000 样本=8000 帧=0.5s
    w2 = asr_mod.wav_to_float32(raw2)
    assert w2 is not None and len(w2) == 8000
    # 非 16bit → None
    raw3 = _make_wav(_silence(0.5), width=1)
    assert asr_mod.wav_to_float32(raw3) is None


def test_disabled_without_dep(monkeypatch, tmp_path):
    monkeypatch.setattr(asr_mod, "_HAS_DEP", False)
    c = asr_mod.AsrClient()
    assert not c.enabled
    assert c.recognize(_make_wav(_silence(0.5))) is None


def test_recognize_returns_cleaned_text(monkeypatch, tmp_path):
    """模型就绪 → 识别结果剥标签返回；太短/失败 → None。"""
    calls = {}

    class _FakeModel:
        def __call__(self, waveform, **kw):
            calls["waveform"] = waveform
            calls["kwargs"] = kw
            return ["<|zh|><|NEUTRAL|><|Speech|>你好，小助。"]

    c = asr_mod.AsrClient()
    c._model = _FakeModel()
    text = c.recognize(_make_wav(_silence(0.5)))
    assert text == "你好，小助。"
    assert calls["kwargs"] == {"language": "zh", "textnorm": "withitn"}
    # 太短 → None
    assert c.recognize(_make_wav(_silence(0.1))) is None
    # 模型未就绪 → None（不抛错）
    c._model = None
    assert c.recognize(_make_wav(_silence(0.5))) is None


def test_recognize_model_failure(monkeypatch, tmp_path):
    c = asr_mod.AsrClient()

    def boom(waveform, **kw):
        raise RuntimeError("infer failed")

    c._model = type("M", (), {"__call__": boom})()
    assert c.recognize(_make_wav(_silence(0.5))) is None
