"""语音合成：本地 Kokoro-82M-zh（默认，免费离线）+ DashScope 云引擎 + 小米 MiMo TTS。

引擎选择：PLANNER_TTS_ENGINE = local（默认）| cloud | mimo。
- local：Kokoro v1.1-zh onnx（onnxruntime 推理，无 torch）——中文音素用 misaki[zh]，
  模型缓存 ~/.cache/planner_tts（共享，dev/release 共用，下载一次离线可用）
- cloud：DashScope Qwen-Audio-TTS/CosyVoice（需 PLANNER_TTS_API_KEY）
- mimo：小米 MiMo TTS（OpenAI 兼容 /v1/chat/completions，需 PLANNER_MIMO_API_KEY；
  TTS 系列当前限时免费）

引擎不可用/合成失败 → enabled=False 或返回 None，全链路静默降级（不朗读、不影响生成）。

合成流程：LLM 完整文本收束 → synthesize_async（后台线程）→
音频存 data/tts/{uuid}.wav → push {type: "audio", url: "/tts/{name}"} 事件，
由主进程在悬浮球形态时转给气泡窗口播放（面板展开时丢弃，只读气泡）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from pathlib import Path

_logger = logging.getLogger("planner.tts")

try:
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer

    _HAS_DASHSCOPE = True
except Exception:  # SDK 缺失：云引擎不可用
    dashscope = None
    SpeechSynthesizer = None
    _HAS_DASHSCOPE = False

# 链接 [文字](url) → 保留文字；其余 markdown 符号删掉，避免被 TTS 读出来
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MAX_TEXT = 2000   # 单次合成文本上限（本地推理留余量）

# Kokoro 本地引擎：共享缓存（与 ASR 模型同模式，dev/release 共用）
_DEFAULT_TTS_CACHE = Path(os.getenv("PLANNER_TTS_CACHE", "")) if os.getenv("PLANNER_TTS_CACHE") \
    else Path.home() / ".cache" / "planner_tts"
_KOKORO_MODEL_DIR = (_DEFAULT_TTS_CACHE /
                     "models" / "onnx-community--Kokoro-82M-v1.1-zh-ONNX" / "snapshots" / "master")
# 声调箭头 → 数字（Kokoro v1.1-zh：1=阴平 2=阳平 3=上声 4=去声 5=轻声）
_TONE_MAP = {"\u2192": "1", "\u2197": "2", "\u2193": "3", "\u2198": "4"}
# 精确码点映射（misaki 输出中 vocab 缺失的字符 → 最近似音）
_CHAR_MAP = {
    "\uff0c": ",",          # ，
    "\u0264": "o",          # ɤ → o
    "\u027b": "\u0292",     # ʕ → ʐ
    "\uab67": "\u03c7",     # ꭓ → χ
}

# 小米 MiMo TTS 预置音色（官方文档 2026-08 版本）
_MIMO_VOICES = [
    {"id": "mimo_default", "label": "MiMo 默认（中国集群=冰糖）"},
    {"id": "冰糖", "label": "冰糖 · 中文女声"},
    {"id": "茉莉", "label": "茉莉 · 中文女声"},
    {"id": "苏打", "label": "苏打 · 中文男声"},
    {"id": "白桦", "label": "白桦 · 中文男声"},
    {"id": "Mia", "label": "Mia · 英文女声"},
    {"id": "Chloe", "label": "Chloe · 英文女声"},
    {"id": "Milo", "label": "Milo · 英文男声"},
    {"id": "Dean", "label": "Dean · 英文男声"},
]


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


# ── 本地引擎：Kokoro-82M-zh（onnx，无 torch）────────────────

_MAX_TOKENS = 508   # voice style 矩阵 510 行，留 2 个 padding（[0, *tokens, 0]）

class _KokoroLocal:
    """Kokoro zh 本地合成（懒加载单例：misaki 音素 → 声调数字 → 字符 vocab → onnx）。"""

    def __init__(self, model_dir: Path, voice: str = "zf_001") -> None:
        self.model_dir = Path(model_dir)
        self.voice = voice
        self._sess = None
        self._vocab = None
        self._g2p = None
        self._voices = None
        self._error = ""
        self._provider = ""
        self._lock = threading.Lock()   # 初始化/合成串行化（会话线程与 HTTP 线程并发安全）

    @property
    def ready(self) -> bool:
        return self._sess is not None

    def _ensure(self):
        if self._sess is not None:
            return True
        try:
            import json
            import numpy as np
            import onnxruntime as ort

            onnx_dir = self.model_dir / "onnx"
            tok_file = self.model_dir / "tokenizer.json"
            voices_dir = self.model_dir / "voices"
            if not onnx_dir.is_dir() or not tok_file.is_file() or not voices_dir.is_dir():
                self._error = "模型未下载"
                _logger.info("[tts] 本地引擎未就绪：%s", self._error)
                return False
            self._vocab = json.load(open(tok_file, encoding="utf-8"))["model"]["vocab"]
            # voices：目录内每个 .bin（原始 float32 510×256）合并为 npz（按音色名）
            merged = _DEFAULT_TTS_CACHE / "kokoro_voices.npz"
            if not merged.is_file():
                voices = {}
                for f in voices_dir.glob("*.bin"):
                    raw = np.frombuffer(f.read_bytes(), dtype=np.float32)
                    voices[f.stem] = raw.reshape(510, 256)
                np.savez(merged, **voices)
            self._voices = np.load(merged)
            # 推理后端：DirectML GPU（FP32，驱动支持则加速）→ CPU FP32 → CPU int8。
            # 平台排雷记录：本机（Core Ultra 5 338H + Arc B370）实测——
            #   - OpenVINO：onnx 前端不支持模型的 SplitToSequence/SequenceAt → 转换失败，死路
            #   - DirectML int8：会话创建卡死；fp16：ConvTranspose 驱动报错（0x80070057，驱动 bug，
            #     更新 Intel 驱动后可能修复——届时本代码自动启用 GPU）
            #   - CPU fp16：onnxruntime CPU 对 fp16 支持不完整 → NaN
            #   - CPU fp32：正常且比 int8 快 ~10x（无 AVX512，int8 反量化开销大）
            ort.set_default_logger_severity(3)   # 只留 ERROR（DML/图优化警告太吵）
            sess, provider = None, ""
            fp32_file = onnx_dir / "model.onnx"
            if fp32_file.is_file():
                if "DmlExecutionProvider" in ort.get_available_providers():
                    try:
                        cand = ort.InferenceSession(
                            str(fp32_file), providers=["DmlExecutionProvider"])
                        if "DmlExecutionProvider" in cand.get_providers():
                            sess, provider = cand, "DirectML-GPU(f32)"
                    except Exception:
                        _logger.info("[tts] DirectML 不可用，回退 CPU(f32)")
                if sess is None:
                    try:
                        sess = ort.InferenceSession(str(fp32_file), providers=["CPUExecutionProvider"])
                        provider = "CPU(f32)"
                    except Exception:
                        _logger.info("[tts] FP32 模型不可用，回退 int8")
            if sess is None:
                sess = ort.InferenceSession(
                    str(onnx_dir / "model_int8.onnx"), providers=["CPUExecutionProvider"])
                provider = "CPU(int8)"
            self._sess = sess
            self._provider = provider
            from misaki import zh as _zh
            self._g2p = _zh.ZHG2P()
            _logger.info("[tts] Kokoro 本地引擎就绪：%s（%s）", self.model_dir, provider)
            return True
        except Exception:
            self._error = "初始化失败"
            _logger.exception("[tts] Kokoro 初始化失败")

    def _phonemes_to_tokens(self, text: str) -> list[int] | None:
        if not text:
            return None
        try:
            p = self._g2p(text)
            p = "".join(_TONE_MAP.get(c, c) for c in p)
            p = "".join(_CHAR_MAP.get(c, c) for c in p)
            p = p.lower()                       # 英文段小写（vocab 兼容）
            tokens = [self._vocab[c] for c in p if c in self._vocab]
            return tokens if tokens else None
        except Exception:
            _logger.exception("[tts] 音素化失败")
            return None

    def synthesize(self, text: str, voice: str | None = None) -> bytes | None:
        """合成 → wav bytes（24kHz 16bit）；失败返回 None。voice 非空时临时换音色。"""
        import numpy as np

        with self._lock:  # 并发调用共享 g2p/session/voice，整体串行化
            if not self._ensure():
                return None
            use_voice = voice or self.voice
            tokens = self._phonemes_to_tokens(text)
            if not tokens:
                return None
            # voice style 矩阵只有 510 行（≈21s 音频上限），超长文本截断（宁短勿崩）
            if len(tokens) > _MAX_TOKENS:
                _logger.info("[tts] 文本过长，音素 %d → 截断至 %d", len(tokens), _MAX_TOKENS)
                tokens = tokens[:_MAX_TOKENS]
            try:
                wav = self._run_tokens(tokens, use_voice)
                if len(wav) < 2400:
                    return None
                # float32 → int16 PCM wav（soundfile）
                import io
                import soundfile as sf
                buf = io.BytesIO()
                sf.write(buf, wav, 24000, format="WAV", subtype="PCM_16")
                return buf.getvalue()
            except Exception:
                _logger.exception("[tts] Kokoro 合成失败")
                return None

    def _run_tokens(self, tokens: list[int], use_voice: str) -> np.ndarray:
        """推理 → float32 波形；异常输出（NaN/饱和）或 DML 驱动报错 → 回退 CPU 重试。"""
        import numpy as np

        for attempt in range(2):
            try:
                style = self._voices[use_voice][len(tokens)]
                out, _ = self._sess.run(None, {
                    "input_ids": np.array([[0, *tokens, 0]], dtype=np.int64),
                    "style": np.array(style, dtype=np.float32)[None, :],
                    "speed": np.array([1.0], dtype=np.float32),
                })
                wav = np.asarray(out, dtype=np.float32).reshape(-1)
                rms = float(np.sqrt(np.mean(wav ** 2)))
                if not np.isfinite(rms) or rms > 0.99:
                    raise RuntimeError(f"推理输出异常（rms={rms}）")
                return wav
            except Exception:
                if attempt == 0 and self._provider.startswith("DirectML"):
                    # DML 驱动不支持（ConvTranspose 报错）或输出异常 → 回退 CPU(f32)
                    _logger.warning("[tts] DirectML 推理异常，回退 CPU(f32)")
                    import onnxruntime as _ort
                    fp32_file = self.model_dir / "onnx" / "model.onnx"
                    self._sess = _ort.InferenceSession(
                        str(fp32_file), providers=["CPUExecutionProvider"])
                    self._provider = "CPU(f32)"
                    continue
                raise


class TtsClient:
    """语音合成客户端（engine: local=Kokoro / cloud=DashScope / mimo=小米 MiMo）。"""

    def __init__(self, data_root: Path, engine: str = "local",
                 api_key: str = "", model: str = "", voice: str = "zf_001",
                 base_url: str = "") -> None:
        self.engine = engine
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.base_url = base_url
        self.tts_dir = Path(data_root) / "tts"
        self._lock = threading.Lock()
        self._local = _KokoroLocal(_KOKORO_MODEL_DIR, voice)
        if engine == "local":
            self._engine_ok = True      # 懒加载，可用性在首次合成时确认
            self._enabled = True        # 自动播报开关（设置里可关；手动喇叭不受限）
        elif engine == "mimo":
            # 小米 MiMo TTS：OpenAI 兼容 HTTP 接口，无需额外 SDK
            self.model = model or "mimo-v2.5-tts"
            self.voice = voice or "mimo_default"
            self.base_url = base_url or "https://api.xiaomimimo.com/v1"
            self._engine_ok = bool(api_key)
            self._enabled = self._engine_ok
            if not self._engine_ok:
                _logger.info("[tts] MiMo 引擎未启用：未配置 PLANNER_MIMO_API_KEY")
        else:
            self._engine_ok = bool(api_key and _HAS_DASHSCOPE)
            self._enabled = self._engine_ok
            if not self._engine_ok:
                _logger.info("[tts] 云引擎未启用：%s",
                             "未配置 PLANNER_TTS_API_KEY" if not api_key else "dashscope SDK 缺失")

    @property
    def enabled(self) -> bool:
        """自动播报开关（关闭后仅手动喇叭/试听可用）。"""
        return self._enabled

    def list_voices(self) -> list[dict]:
        """可用音色列表（按引擎返回：Kokoro 本地音色 / MiMo 预置音色）。"""
        if self.engine == "mimo":
            return list(_MIMO_VOICES)
        voices_dir = _KOKORO_MODEL_DIR / "voices"
        if not voices_dir.is_dir():
            return []
        names = sorted(f.stem for f in voices_dir.glob("*.bin"))
        out = []
        for n in names:
            if n.startswith("zf"):
                label = f"{n} · 女声"
            elif n.startswith("zm"):
                label = f"{n} · 男声"
            else:
                label = n
            out.append({"id": n, "label": label})
        return out

    def synthesize(self, text: str, voice: str | None = None) -> str | None:
        """阻塞合成整句 → 保存音频 → 返回 /tts/{name} URL；失败/引擎不可用返回 None。
        手动喇叭/试听不受自动播报开关（_enabled）限制；voice 非空时临时用该音色。"""
        if not self._engine_ok:
            return None
        content = clean_speech_text(text)
        if not content:
            return None
        use_voice = voice or self.voice
        try:
            if self.engine == "local":
                audio = self._local.synthesize(content, voice=use_voice)
                ext = ".wav"
            elif self.engine == "mimo":
                audio = self._synthesize_mimo(content, use_voice)
                ext = ".wav"
            else:
                dashscope.api_key = self.api_key
                synthesizer = SpeechSynthesizer(model=self.model, voice=self.voice)
                audio = synthesizer.call(content)
                ext = ".mp3"
            if not audio:
                return None
            self.tts_dir.mkdir(parents=True, exist_ok=True)
            name = uuid.uuid4().hex + ext
            with self._lock:
                (self.tts_dir / name).write_bytes(audio)
            _logger.info("[tts] 合成完成 %s（%d 字节）", name, len(audio))
            return "/tts/" + name
        except Exception:
            _logger.exception("[tts] 合成失败")
            return None

    def _synthesize_mimo(self, text: str, voice: str) -> bytes | None:
        """调用小米 MiMo TTS（OpenAI 兼容 /chat/completions）→ wav bytes。"""
        import base64
        import json

        import httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user",
                 "content": "请用自然、亲切、清晰的中文语气朗读。"},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "wav", "voice": voice},
        }
        try:
            r = httpx.post(
                url,
                headers={
                    "api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
            return base64.b64decode(audio_b64)
        except Exception:
            _logger.exception("[tts] MiMo 合成失败")
            return None

    def synthesize_async(self, text: str, on_done) -> None:
        """后台线程合成；on_done(url) 回调（url=None 表示失败/关闭/引擎不可用）。
        自动播报路径：受 _enabled（设置开关）与引擎可用性双重限制。"""
        if not self._enabled or not self._engine_ok:
            on_done(None)
            return

        def _run() -> None:
            on_done(self.synthesize(text))

        threading.Thread(target=_run, name="planner-tts", daemon=True).start()
