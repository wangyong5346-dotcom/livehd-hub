"""核心数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Stream:
    url: str
    name: str = ""
    group: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    tvg_logo: str = ""
    source_name: str = ""
    claimed_height: int = 0
    attrs: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.url.strip()


@dataclass
class Channel:
    name: str
    group: str = "其他"
    logo: str = ""
    tvg_id: str = ""
    streams: list[Stream] = field(default_factory=list)


@dataclass
class QuickResult:
    ok: bool = False
    latency_ms: float = 0.0
    speed_mbps: float = 0.0
    status: int = 0
    content_type: str = ""
    bytes_read: int = 0
    playlist_height: int = 0
    playlist_codec: str = ""
    variant_urls: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
            "speed_mbps": round(self.speed_mbps, 2),
            "status": self.status,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "playlist_height": self.playlist_height,
            "error": self.error,
        }


@dataclass
class DeepResult:
    ok: bool = False
    width: int = 0
    height: int = 0
    codec: str = ""
    fps: float = 0.0
    error: str = ""


@dataclass
class ScoredStream:
    stream: Stream
    score: float = 0.0
    success_rate: float = 0.0
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    speed_mbps: float = 0.0
    width: int = 0
    height: int = 0
    resolution_source: str = "unknown"
    codec: str = ""
    history_score: float = 0.0
    rounds_ok: int = 0
    rounds_total: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.stream.name,
            "group": self.stream.group,
            "url": self.stream.url,
            "score": round(self.score, 2),
            "resolution": f"{self.width}x{self.height}" if self.width else "unknown",
            "resolution_source": self.resolution_source,
            "latency_ms": round(self.latency_ms, 1),
            "jitter_ms": round(self.jitter_ms, 1),
            "speed_mbps": round(self.speed_mbps, 2),
            "success_rate": round(self.success_rate, 3),
            "rounds": f"{self.rounds_ok}/{self.rounds_total}",
            "history": round(self.history_score, 3),
            "codec": self.codec,
            "reasons": self.reasons,
        }
