"""语音输入：SenseVoiceSmall-onnx 本地识别（onnxruntime，无 torch）。

链路：前端录音 → 16k mono PCM wav → POST /asr → 后端 wave 解析 →
funasr_onnx.SenseVoiceSmall（int8 量化版，常驻单例，识别串行化）→ 文本。

模型：首次启动后台自动从 ModelScope 下载 iic/SenseVoiceSmall-onnx（~230MB）
并补充原版仓库的 sentencepiece bpe 文件（funasr_onnx 依赖，~370KB）；
下载/加载在后台线程完成，模型就绪前识别返回 None（静默降级）。
funasr_onnx/onnxruntime 未安装 → AsrClient.enabled=False，前端不显示入口。
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import wave
from pathlib import Path

import numpy as np

_logger = logging.getLogger("planner.asr")

try:
    import httpx
    from funasr_onnx import SenseVoiceSmall

    _HAS_DEP = True
except Exception:  # 依赖缺失：语音输入关闭，其余功能不受影响
    SenseVoiceSmall = None
    _HAS_DEP = False

_MODEL_ID = "iic/SenseVoiceSmall-onnx"
_BPE_NAME = "chn_jpn_yue_eng_ko_spectok.bpe.model"
_BPE_URL = ("https://www.modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/" + _BPE_NAME)
# 模型为只读共享资源：统一缓存到 ~/.cache/planner_asr（dev/release 共用，
# 下载一次即离线可用；PLANNER_ASR_CACHE 可覆盖）。不随 data/ 隔离重复下载。
_DEFAULT_CACHE = Path(os.getenv("PLANNER_ASR_CACHE", "")) if os.getenv("PLANNER_ASR_CACHE") \
    else Path.home() / ".cache" / "planner_asr"
# onnx 仓库文件清单（7 个）+ 原版仓库补充的 bpe 文件
_MODEL_FILES = ["model_quant.onnx", "config.yaml", "am.mvn", "tokens.json",
                "configuration.json", "README.md", ".gitattributes", _BPE_NAME]
_TARGET_SR = 16000
_MAX_WAV_BYTES = 20 * 1024 * 1024

_TAG_RE = re.compile(r"<\|[^|]*\|>")


def strip_tags(text: str) -> str:
    """去掉 SenseVoice 输出标签（<|zh|><|NEUTRAL|><|Speech|>…），保留正文。"""
    return _TAG_RE.sub("", text).strip()


def wav_to_float32(wav_bytes: bytes) -> np.ndarray | None:
    """解析 wav（标准库 wave）→ float32 单声道波形，重采样到 16k。

    仅支持 16-bit PCM（前端约定输出格式）；解析失败/格式不符返回 None。
    """
    if not wav_bytes or len(wav_bytes) > _MAX_WAV_BYTES:
        return None
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sr = wf.getframerate()
            nch = wf.getnchannels()
            width = wf.getsampwidth()
            data = wf.readframes(wf.getnframes())
    except Exception:
        return None
    if width != 2 or sr <= 0:
        return None
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        samples = samples[::nch]  # 取第一声道
    if len(samples) == 0:
        return None
    if sr != _TARGET_SR:
        n_out = max(1, int(len(samples) * _TARGET_SR / sr))
        idx = np.linspace(0, len(samples) - 1, n_out)
        samples = np.interp(idx, np.arange(len(samples)), samples)
    return samples.astype(np.float32)


class AsrClient:
    """SenseVoiceSmall-onnx 本地语音识别（懒加载单例，识别串行化）。

    auto_prepare：默认 = 非 mock 模式才自动下载/加载模型
    （mock/测试环境不触碰网络，模型由测试注入）。
    """

    def __init__(self, model_id: str = _MODEL_ID,
                 auto_prepare: bool | None = None) -> None:
        self.model_id = model_id
        self._model = None
        self._lock = threading.Lock()
        self._load_error = ""
        self._enabled = _HAS_DEP
        if not self._enabled:
            _logger.info("[asr] 语音输入未启用：funasr_onnx 未安装")
            return
        if auto_prepare is None:
            from . import config as _config
            auto_prepare = not _config.PLANNER_MOCK_LLM
        if auto_prepare:
            # 模型未就绪：后台下载 + 加载（不阻塞启动，幂等：已缓存则秒回）
            threading.Thread(target=self._prepare, name="planner-asr-prep", daemon=True).start()

    @property
    def enabled(self) -> bool:
        """依赖是否可用（模型可能在后台下载中）。"""
        return self._enabled

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def model_dir(self) -> Path:
        """模型缓存目录（存在即视为模型已下载）。"""
        return _DEFAULT_CACHE / "SenseVoiceSmall-onnx"

    def _ensure_downloaded(self) -> Path:
        """确保模型文件齐全（缺哪个下哪个，可续），返回模型目录。"""
        d = self.model_dir
        missing = [f for f in _MODEL_FILES if not (d / f).is_file()]
        if not missing:
            return d
        d.mkdir(parents=True, exist_ok=True)
        for name in missing:
            url = _BPE_URL if name == _BPE_NAME else (
                f"https://www.modelscope.cn/models/{self.model_id}/resolve/master/{name}")
            _logger.info("[asr] 下载模型文件 %s ...", name)
            r = httpx.get(url, timeout=900, follow_redirects=True)
            r.raise_for_status()
            tmp = d / (name + ".part")
            tmp.write_bytes(r.content)
            tmp.replace(d / name)
        return d

    def _prepare(self) -> None:
        """后台下载模型 + 补 bpe 文件 + 加载（幂等，仅一次）。"""
        try:
            d = self._ensure_downloaded()
            self._model = SenseVoiceSmall(model_dir=str(d), quantize=True, batch_size=1)
            _logger.info("[asr] 模型就绪：%s", d)
        except Exception:
            self._load_error = "模型下载/加载失败"
            _logger.exception("[asr] 模型准备失败")

    def recognize(self, wav_bytes: bytes) -> str | None:
        """识别 wav → 纯文本（剥 SenseVoice 标签）；失败/未就绪返回 None。"""
        if not self._enabled:
            return None
        waveform = wav_to_float32(wav_bytes)
        if waveform is None or len(waveform) < _TARGET_SR * 0.5:  # 太短（<0.5s）无意义
            return None
        with self._lock:
            model = self._model
        if model is None:
            if self._load_error:
                _logger.warning("[asr] 识别跳过：%s", self._load_error)
            return None
        try:
            raw = model(waveform, language="zh", textnorm="withitn")[0]
            text = strip_tags(raw)
            if not text:
                return None
            _logger.info("[asr] 识别：%s", text[:60])
            return text
        except Exception:
            _logger.exception("[asr] 识别异常")
            return None
