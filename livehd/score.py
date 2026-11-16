"""评分与硬门槛：分辨率 / 延迟 / 速率 / 稳定性 / 历史 五维加权。"""

from __future__ import annotations

from .models import ScoredStream
from .util import clamp, host_of


def resolution_score(height: int) -> float:
    if height >= 2160:
        return 1.0
    if height >= 1080:
        return 0.85 + 0.15 * clamp((height - 1080) / 1080)
    if height >= 720:
        return 0.6
    return 0.0


def latency_score(latency_ms: float, max_latency_ms: float) -> float:
    if latency_ms <= 0:
        return 0.0
    return clamp(1.0 - latency_ms / max(1.0, max_latency_ms))


def speed_score(speed_mbps: float) -> float:
    return clamp(speed_mbps / 8.0)


def stability_score(success_rate: float, jitter_ms: float) -> float:
    return clamp(success_rate * (1.0 - clamp(jitter_ms / 500.0)))


def compute_score(item: ScoredStream, cfg) -> float:
    sel, q = cfg["selection"], cfg["quality"]
    weights = sel["weights"]
    parts = {
        "resolution": resolution_score(item.height),
        "latency": latency_score(item.latency_ms, q["max_latency_ms"]),
        "speed": speed_score(item.speed_mbps),
        "stability": stability_score(item.success_rate, item.jitter_ms),
        "history": clamp(item.history_score),
    }
    return round(100 * sum(weights.get(k, 0) * v for k, v in parts.items()), 2)


def hard_gate(item: ScoredStream, cfg) -> tuple[bool, list[str]]:
    q = cfg["quality"]
    reasons: list[str] = []
    min_height = int(q["min_height"])
    if item.height <= 0:
        reasons.append("分辨率未知")
    elif item.height < min_height:
        reasons.append(f"分辨率 {item.height}p < {min_height}p")
    elif item.width and item.width < int(q["min_width"]):
        reasons.append(f"宽度 {item.width} < {q['min_width']}")
    if item.latency_ms > float(q["max_latency_ms"]):
        reasons.append(f"延迟 {item.latency_ms:.0f}ms 超限")
    if item.speed_mbps < float(q["min_speed_mbps"]):
        reasons.append(f"速率 {item.speed_mbps:.2f}Mbps 不足")
    if item.success_rate < float(q["min_success_rate"]):
        reasons.append(f"成功率 {item.success_rate:.2f} 不足")
    if not item.rounds_ok:
        reasons.append("全部轮次失败")
    return (not reasons), reasons


def select_top(items: list[ScoredStream], cfg) -> list[ScoredStream]:
    """每频道取 Top-N，并限制同一主机数量，避免单点故障导致的假冗余。"""
    sel = cfg["selection"]
    top_n = max(1, int(sel["top_n"]))
    max_per_host = max(1, int(sel["max_per_host"]))
    chosen: list[ScoredStream] = []
    per_host: dict[str, int] = {}
    for item in sorted(items, key=lambda x: (-x.score, x.latency_ms)):
        host = host_of(item.stream.url)
        if per_host.get(host, 0) >= max_per_host:
            item.reasons.append("同主机源数量超限")
            continue
        per_host[host] = per_host.get(host, 0) + 1
        chosen.append(item)
        if len(chosen) >= top_n:
            break
    return chosen
