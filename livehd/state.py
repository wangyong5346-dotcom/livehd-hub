"""历史质量状态：EWMA 成功率、连续失败隔离、跨轮次缓存。

作用：把「单次测速」升级为「跨天稳定性记忆」——连续失败的源进入隔离区，
不再浪费每轮探测预算；历史表现好的源优先测、优先入选。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .util import LOG, clamp, url_hash

ALPHA = 0.3


class QualityState:
    def __init__(self, path: str | Path, quarantine_failures: int = 4, quarantine_hours: float = 6):
        self.path = Path(path)
        self.quarantine_failures = quarantine_failures
        self.quarantine_seconds = quarantine_hours * 3600
        self.data: dict = {"version": 1, "updated": 0, "entries": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded.get("entries"), dict):
                    self.data = loaded
            except Exception as exc:
                LOG.warning("状态文件损坏，将重建: %s", exc)

    def entry(self, url: str) -> dict:
        return self.data["entries"].get(url_hash(url), {})

    def history_score(self, url: str) -> float:
        e = self.entry(url)
        if not e or not e.get("runs"):
            return 0.5  # 新源给中性分，避免饿死
        return clamp(float(e.get("ewma_success", 0.5)))

    def is_quarantined(self, url: str) -> bool:
        e = self.entry(url)
        if int(e.get("consecutive_failures", 0)) < self.quarantine_failures:
            return False
        last_fail = float(e.get("last_fail", 0))
        return (time.time() - last_fail) < self.quarantine_seconds

    def update(self, url: str, ok: bool, latency_ms: float = 0, speed_mbps: float = 0, height: int = 0) -> None:
        key = url_hash(url)
        e = dict(self.data["entries"].get(key, {}))
        runs = int(e.get("runs", 0)) + 1
        prev = float(e.get("ewma_success", 0.5))
        ewma = round(ALPHA * (1.0 if ok else 0.0) + (1 - ALPHA) * prev, 4)
        e.update(
            {
                "runs": runs,
                "success": int(e.get("success", 0)) + (1 if ok else 0),
                "ewma_success": ewma,
                "consecutive_failures": 0 if ok else int(e.get("consecutive_failures", 0)) + 1,
                "last_seen": int(time.time()),
                "avg_latency_ms": round(
                    (float(e.get("avg_latency_ms", 0)) * (runs - 1) + latency_ms) / runs, 1
                ) if ok else float(e.get("avg_latency_ms", 0)),
                "avg_speed_mbps": round(
                    (float(e.get("avg_speed_mbps", 0)) * (runs - 1) + speed_mbps) / runs, 2
                ) if ok else float(e.get("avg_speed_mbps", 0)),
                "height": height or int(e.get("height", 0)),
            }
        )
        if ok:
            e["last_ok"] = int(time.time())
        else:
            e["last_fail"] = int(time.time())
        self.data["entries"][key] = e

    def prune(self, keep_hashes: set[str], max_age_days: int = 30) -> int:
        cutoff = time.time() - max_age_days * 86400
        before = len(self.data["entries"])
        self.data["entries"] = {
            k: v for k, v in self.data["entries"].items()
            if k in keep_hashes or float(v.get("last_seen", 0)) > cutoff
        }
        return before - len(self.data["entries"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = int(time.time())
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("质量状态已保存：%d 条源记录 -> %s", len(self.data["entries"]), self.path)
