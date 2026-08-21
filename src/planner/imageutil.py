"""图片预处理：任意常见格式 → 视觉模型可用的 base64 data URL。

DeepSeek vision-exp 支持 JPEG/PNG/GIF/WebP（bmp/tiff/ico 等需转换），
单图 ≤32MiB、请求体 ≤48MiB。统一转 PNG + 超长边缩放，控制体积与 token。
GIF 只取首帧（cv2.imdecode 默认行为）。
"""

from __future__ import annotations

import base64
import logging

import numpy as np

_logger = logging.getLogger("planner.imageutil")

_MAX_PX = 2000                # 单边最大像素（视觉模型会再自动缩放，此处防请求体过大）
_MAX_B64 = 24 * 1024 * 1024   # base64 长度上限（留余量给 48MiB 请求体限制）


def image_to_data_url(path) -> str | None:
    """图片文件 → data:image/png;base64,xxx；读取/解码失败返回 None。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        _logger.warning("[vision] 读取图片失败: %s", path)
        return None
    if not raw:
        return None
    import cv2

    try:
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is None:
        _logger.warning("[vision] 图片解码失败: %s", path)
        return None
    scale = 1.0
    for _ in range(4):   # 过大则逐级缩小重试（最多缩 4 次）
        h, w = img.shape[:2]
        s = scale
        if max(h, w) * s > _MAX_PX:
            s = _MAX_PX / max(h, w)
        if s != 1.0:
            resized = cv2.resize(img, (int(w * s), int(h * s)),
                                 interpolation=cv2.INTER_AREA)
        else:
            resized = img
        ok, buf = cv2.imencode(".png", resized)
        if not ok:
            return None
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        if len(b64) <= _MAX_B64:
            return "data:image/png;base64," + b64
        scale *= 0.5
    _logger.warning("[vision] 图片压缩后仍超限: %s", path)
    return None
