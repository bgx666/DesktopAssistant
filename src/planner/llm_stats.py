"""LLM 用量与缓存命中统计（data/llm_stats.json，环形保留最近 N 轮生成）。

每轮生成记一条：输入/输出 token、缓存命中（DeepSeek 的 prompt_cache_hit_tokens
经 llm.py shim 映射进 usage_metadata.input_token_details.cache_read）、耗时、TPS。
`cache_known=False` 表示端点没报缓存字段（此时命中率不计入汇总，避免误报 0%）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

_logger = logging.getLogger("planner.llm_stats")

#: 环形保留的生成轮数（统计面板窗口）
MAX_RECORDS = 50


class LlmStats:
    """最近 N 轮生成的用量/缓存统计（JSON 文件，随 PLANNER_DATA_ROOT 隔离）。"""

    def __init__(self, data_root: Path, max_records: int = MAX_RECORDS) -> None:
        self.path = Path(data_root) / "llm_stats.json"
        self.max_records = max(1, int(max_records))
        self._lock = threading.RLock()
        self._records: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [r for r in data if isinstance(r, dict)][-self.max_records:]
        except Exception:
            _logger.warning("[llm_stats] 读取失败，从空开始")
        return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._records, ensure_ascii=False),
                                 encoding="utf-8")
        except OSError:
            _logger.warning("[llm_stats] 写入失败")

    def record(self, *, input_tokens: int, output_tokens: int,
               cache_hit: int = 0, cache_known: bool = False,
               reason_tokens: int = 0,
               duration_s: float = 0.0, provider: str = "", model: str = "",
               rounds: int = 1, mock: bool = False) -> dict:
        """记一轮生成。返回该条记录。"""
        duration = max(0.0, float(duration_s or 0.0))
        out = int(max(0, output_tokens or 0))
        rec = {
            "ts": time.time(),
            "in": int(max(0, input_tokens or 0)),
            "out": out,
            "reason": int(max(0, reason_tokens or 0)),
            "hit": int(max(0, cache_hit or 0)),
            "cache_known": bool(cache_known),
            "ms": int(duration * 1000),
            "tps": round(out / duration, 1) if duration > 0 else 0.0,
            "provider": str(provider or ""),
            "model": str(model or ""),
            "rounds": int(max(1, rounds or 1)),
            "mock": bool(mock),
        }
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self.max_records:
                self._records = self._records[-self.max_records:]
            self._save()
        return rec

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    def summary(self) -> dict:
        """最近窗口汇总：命中率按「报了缓存字段的记录」加权；TPS 取算术平均。"""
        recs = self.list()
        n = len(recs)
        if not n:
            return {"count": 0, "cache_rate": None, "avg_tps": 0.0,
                    "avg_in": 0, "avg_out": 0, "window": self.max_records, "last": None}
        known = [r for r in recs if r.get("cache_known")]
        prompt = sum(r["in"] for r in known)
        hit = sum(r["hit"] for r in known)
        return {
            "count": n,
            "window": self.max_records,
            "cache_rate": round(hit / prompt * 100, 1) if prompt > 0 else None,
            "avg_tps": round(sum(r["tps"] for r in recs) / n, 1),
            "avg_in": round(sum(r["in"] for r in recs) / n),
            "avg_out": round(sum(r["out"] for r in recs) / n),
            "avg_reason": round(sum(r.get("reason", 0) for r in recs) / n),
            "last": recs[-1],
        }
