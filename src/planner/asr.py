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
# 纯标点/空格/符号清洗用：去掉非文字字符
_NON_TEXT_RE = re.compile(r"[\W_]+", re.UNICODE)
# 常见语气词/口头禅：单独出现时不算有效语音输入
_FILLER_CHARS = set("嗯啊哦呃唉哼哈呀吧呢嘛咯呗唔诶哎")


def strip_tags(text: str) -> str:
    """去掉 SenseVoice 输出标签（<|zh|><|NEUTRAL|><|Speech|>…），保留正文。"""
    return _TAG_RE.sub("", text).strip()


def _is_useful_text(text: str) -> bool:
    """判断 ASR 结果是否值得触发 LLM。

    过滤：
    - 空文本
    - 只有标点/符号（如“。”）
    - 只有语气词（如“嗯”“啊”“哦”）
    """
    t = _NON_TEXT_RE.sub("", text or "").strip()
    if not t:
        return False
    if len(t) <= 3 and all(ch in _FILLER_CHARS for ch in t):
        return False
    return True


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
        self._prepare_started = False
        if not self._enabled:
            _logger.info("[asr] 语音输入未启用：funasr_onnx 未安装")
            return
        # 默认不在构造时自动加载：由 server.main 在 HTTP 服务监听后再调用
        # start_prepare()，避免模型初始化拖慢 /init 与首个 /dequeue。
        # 保留 auto_prepare=True 作为显式立即预加载的兼容入口。
        if auto_prepare is True:
            self.start_prepare()

    def start_prepare(self) -> None:
        """启动后台模型预加载（幂等）。

        模型仍在启动阶段加载，但调用方应确保 HTTP 服务已先监听，
        这样红点变绿不会被模型初始化阻塞。
        """
        if not self._enabled:
            return
        with self._lock:
            if self._prepare_started:
                return
            self._prepare_started = True
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
            if not _is_useful_text(text):
                _logger.info("[asr] 识别结果无效，丢弃：%r", text[:60])
                return None
            _logger.info("[asr] 识别：%s", text[:60])
            return text
        except Exception:
            _logger.exception("[asr] 识别异常")
            return None
