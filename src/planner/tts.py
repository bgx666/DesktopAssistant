"""语音合成：DashScope Qwen-Audio-TTS/CosyVoice（整句合成，非流式）。

启用条件：配置了 PLANNER_TTS_API_KEY（或 DASHSCOPE_API_KEY）且 dashscope SDK 可用；
否则 TtsClient.enabled=False，全链路静默降级（不朗读、不影响生成）。

合成流程：LLM 完整文本收束 → synthesize_async（后台线程，call() 阻塞）→
音频存 data/tts/{uuid}.mp3 → push {type: "audio", url: "/tts/{name}"} 事件，
由主进程在悬浮球形态时转给气泡窗口播放（面板展开时丢弃，只读气泡）。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from pathlib import Path

_logger = logging.getLogger("planner.tts")

try:
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer

    _HAS_DASHSCOPE = True
except Exception:  # SDK 缺失：语音关闭，其余功能不受影响
    dashscope = None
    SpeechSynthesizer = None
    _HAS_DASHSCOPE = False

# 链接 [文字](url) → 保留文字；其余 markdown 符号删掉，避免被 TTS 读出来
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MAX_TEXT = 2000   # 非流式单次上限 20000 字符，本地再留余量


def clean_speech_text(text: str) -> str:
    """清洗 markdown/控制符号 → 适合朗读的纯文本（多行合并为句号连接）。"""
    if not text:
        return ""
    t = str(text)
    t = _MD_LINK_RE.sub(r"\1", t)                # 链接保留文字
    for ch in ("**", "```", "`", "#", "*", "_", "~", ">"):
        t = t.replace(ch, "")
    t = t.replace("\r", " ").replace("\n\n", "\n")
    t = re.sub(r"\s{2,}", " ", t)              # 符号删除残留的多余空格
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    return "。".join(lines)[: _MAX_TEXT]


class TtsClient:
    """整句语音合成（非流式 call）。每次合成新建 SpeechSynthesizer 实例。"""

    def __init__(self, api_key: str, model: str, voice: str, data_root: Path) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.tts_dir = Path(data_root) / "tts"
        self._lock = threading.Lock()
        self._enabled = bool(api_key and _HAS_DASHSCOPE)
        if not self._enabled:
            _logger.info("[tts] 语音未启用：%s",
                         "未配置 PLANNER_TTS_API_KEY" if not api_key else "dashscope SDK 缺失")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def synthesize(self, text: str) -> str | None:
        """阻塞合成整句 → 保存 mp3 → 返回 /tts/{name} URL；失败/未启用返回 None。"""
        if not self._enabled:
            return None
        content = clean_speech_text(text)
        if not content:
            return None
        try:
            dashscope.api_key = self.api_key
            synthesizer = SpeechSynthesizer(model=self.model, voice=self.voice)
            audio = synthesizer.call(content)
            if not audio:
                return None
            self.tts_dir.mkdir(parents=True, exist_ok=True)
            name = uuid.uuid4().hex + ".mp3"
            with self._lock:
                (self.tts_dir / name).write_bytes(audio)
            _logger.info("[tts] 合成完成 %s（%d 字节）", name, len(audio))
            return "/tts/" + name
        except Exception:
            _logger.exception("[tts] 合成失败")
            return None

    def synthesize_async(self, text: str, on_done) -> None:
        """后台线程合成；on_done(url) 回调（url=None 表示失败/未启用）。"""
        if not self._enabled:
            on_done(None)
            return

        def _run() -> None:
            on_done(self.synthesize(text))

        threading.Thread(target=_run, name="planner-tts", daemon=True).start()
