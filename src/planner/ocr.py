"""图片文字识别（OCR）：RapidOCR（onnxruntime 推理，无 torch）。

服务两条链路：
- 用户拖入的图片文件：解析出文字注入对话（标注来源：图片 xxx）
- capture_screen 工具：截屏 → OCR 识别屏幕文字返回给模型

模型随 rapidocr_onnxruntime 包内置（~15MB），无需额外下载；
RapidOCR 惰性加载（首次 ~0.3s），识别一张 ~1-2s。
"""

from __future__ import annotations

import io
import logging
import threading

import numpy as np

_logger = logging.getLogger("planner.ocr")

try:
    from rapidocr_onnxruntime import RapidOCR

    _HAS_DEP = True
except Exception:  # 依赖缺失：OCR 关闭，其余功能不受影响
    RapidOCR = None
    _HAS_DEP = False


class OcrClient:
    """RapidOCR 封装（惰性加载单例，识别串行化）。"""

    def __init__(self) -> None:
        self._ocr = None
        self._lock = threading.Lock()
        self._load_error = ""
        self._enabled = _HAS_DEP
        if not self._enabled:
            _logger.info("[ocr] OCR 未启用：rapidocr_onnxruntime 未安装")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def ready(self) -> bool:
        return self._ocr is not None

    def _ensure(self):
        if self._ocr is None and not self._load_error:
            try:
                self._ocr = RapidOCR()
                _logger.info("[ocr] OCR 模型就绪")
            except Exception:
                self._load_error = "OCR 模型加载失败"
                _logger.exception("[ocr] 模型加载失败")
        return self._ocr

    def recognize(self, image_bytes: bytes) -> str | None:
        """识别图片 bytes → 文字（换行连接）；失败/未就绪返回 None。"""
        if not self._enabled or not image_bytes:
            return None
        try:
            img = _decode_image(image_bytes)
            if img is None:
                return None
            with self._lock:
                ocr = self._ensure()
                if ocr is None:
                    return None
                result, _ = ocr(img)
            if not result:
                return None
            lines = [str(item[1]).strip() for item in result if item and item[1]]
            lines = [ln for ln in lines if ln]
            return "\n".join(lines) if lines else None
        except Exception:
            _logger.exception("[ocr] 识别异常")
            return None

    def recognize_path(self, path) -> str | None:
        """识别图片文件 → 文字。"""
        try:
            with open(path, "rb") as f:
                return self.recognize(f.read())
        except OSError:
            _logger.warning("[ocr] 读取图片失败: %s", path)
            return None


def _decode_image(image_bytes: bytes) -> np.ndarray | None:
    """解码图片 bytes → BGR ndarray（cv2.imdecode，支持 png/jpg/webp 等）。"""
    import cv2

    try:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img if img is not None else None
    except Exception:
        return None


def ocr_png_from_screen(screen: np.ndarray) -> str | None:
    """从屏幕像素数组（BGRA，mss 输出）直接 OCR（内部转换，避免临时文件）。"""
    if not _HAS_DEP:
        return None
    import cv2

    try:
        bgra = np.asarray(screen)
        img = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None
        client = _global_client()
        return client.recognize(buf.tobytes())
    except Exception:
        _logger.exception("[ocr] 屏幕 OCR 失败")
        return None


_global = None
_global_lock = threading.Lock()


def _global_client() -> OcrClient:
    """进程级单例（session 与工具共用）。"""
    global _global
    with _global_lock:
        if _global is None:
            _global = OcrClient()
        return _global
