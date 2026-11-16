"""配置加载、默认值合并与路径解析。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .util import LOG

DEFAULTS: dict = {
    "version": 1,
    "sources_file": "config/sources.json",
    "channels_file": "config/channels.json",
    "output_dir": "docs",
    "publish_folder": "docs",
    "state_file": ".state/quality.json",
    "quality": {
        "min_height": 1080,
        "min_width": 1280,
        "min_speed_mbps": 1.0,
        "max_latency_ms": 1500,
        "min_success_rate": 0.6,
        "allow_unverified_resolution": False,
        "trust_playlist_resolution": True,
    },
    "probe": {
        "rounds": 2,
        "round_interval": 1.0,
        "connect_timeout": 5,
        "read_timeout": 8,
        "sample_bytes": 262144,
        "ffprobe_enabled": True,
        "ffprobe_timeout": 12,
        "ffprobe_top_k": 3,
        "expand_hls_variants": False,
        "max_candidates_per_channel": 12,
        "time_budget_seconds": 780,
        "quick_workers": 24,
        "deep_workers": 6,
    },
    "selection": {
        "top_n": 3,
        "max_per_host": 1,
        "quarantine_failures": 4,
        "quarantine_hours": 6,
        "weights": {
            "resolution": 0.30,
            "latency": 0.25,
            "speed": 0.20,
            "stability": 0.15,
            "history": 0.10,
        },
    },
    "output": {
        "m3u": True,
        "txt": True,
        "json": True,
        "html": True,
        "tvg_url": "",
        "show_quality_info": True,
        "timezone": "Asia/Shanghai",
        "title": "LiveHD 高清直播订阅",
    },
    "network": {
        "user_agent": "Mozilla/5.0 (compatible; LiveHDHub/1.0)",
        "allow_local_files": True,
        "max_source_bytes": 8388608,
        "retry": 1,
        "github_token_env": "GITHUB_TOKEN",
        "github_api": "https://api.github.com",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


class Config:
    def __init__(self, settings_path: str | Path, overrides: dict | None = None):
        self.path = Path(settings_path).resolve()
        self.root = self.path.parent.parent
        raw = load_json(self.path) if self.path.exists() else {}
        merged = deep_merge(DEFAULTS, raw)
        if overrides:
            merged = deep_merge(merged, overrides)
        self.data = merged
        self.sources = load_json(self.resolve(merged["sources_file"]))
        self.channels = load_json(self.resolve(merged["channels_file"]))
        self._validate()

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.root / p)

    def _validate(self) -> None:
        q = self.data["quality"]
        if q["min_height"] <= 0:
            raise ValueError("quality.min_height 必须为正整数")
        p = self.data["probe"]
        if p["rounds"] < 1:
            raise ValueError("probe.rounds 至少为 1")
        if p["quick_workers"] < 1 or p["deep_workers"] < 1:
            raise ValueError("并发数必须为正整数")
        w = self.data["selection"]["weights"]
        total = sum(float(v) for v in w.values())
        if total <= 0:
            raise ValueError("评分权重之和必须大于 0")
        for k in w:
            w[k] = float(w[k]) / total

    def __getitem__(self, key: str):
        return self.data[key]

    def token(self) -> str:
        env_name = self.data["network"]["github_token_env"]
        return os.environ.get(env_name, "")

    def apply_cli(self, args) -> None:
        """把命令行覆盖项写回配置。"""
        p, q, s, o = (
            self.data["probe"],
            self.data["quality"],
            self.data["selection"],
            self.data["output"],
        )
        if args.workers:
            p["quick_workers"] = args.workers
        if args.rounds:
            p["rounds"] = args.rounds
        if args.min_height:
            q["min_height"] = args.min_height
        if args.top_n:
            s["top_n"] = args.top_n
        if args.no_ffprobe:
            p["ffprobe_enabled"] = False
        if args.allow_unverified:
            q["allow_unverified_resolution"] = True
        if args.max_candidates:
            p["max_candidates_per_channel"] = args.max_candidates
        if args.time_budget:
            p["time_budget_seconds"] = args.time_budget
        if args.tvg_url:
            o["tvg_url"] = args.tvg_url
        if args.local_sources:
            self.sources["sources"] = [
                {
                    "name": f"CLI:{Path(x).name}",
                    "type": "playlist",
                    "url": x,
                    "format": "auto",
                    "enabled": True,
                }
                for x in args.local_sources
            ]
        LOG.debug("配置加载完成: %s", self.path)
