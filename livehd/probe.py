"""质检层：快速连通/测速 + HLS 解析 + ffprobe 真实分辨率复核。

两阶段漏斗（相对上游项目的主要优化）：
  Stage A 高并发快速探测：连接/首字节延迟、下载速率、HLS master 里的 RESOLUTION；
  Stage B 低并发 ffprobe 精测：只对 Stage A 通过且每频道 Top-K 的源拉流，读取真实 width/height。
这样既保证「1080p 及以上」不是靠 URL 里的字样猜，又把 Actions 运行时长压在预算内。
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
import time
import urllib.request
from typing import Iterable

from .models import DeepResult, QuickResult, Stream
from .util import LOG, absolute_url

SEGMENT_RE = re.compile(r"\.(ts|m4s|mp4|flv)(\?|$)", re.I)
RESOLUTION_RE = re.compile(r"RESOLUTION=(\d{2,5})[xX](\d{2,5})")
CODECS_RE = re.compile(r'CODECS="([^"]*)"')


def has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def _headers_for(stream: Stream, cfg) -> dict[str, str]:
    net = cfg["network"]
    headers = {"User-Agent": net["user_agent"], "Accept": "*/*"}
    extra = stream.attrs.get("_headers") if isinstance(stream.attrs, dict) else None
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def _open(url: str, cfg, timeout: float, headers: dict[str, str]):
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _download_probe(url: str, cfg, timeout: float, headers: dict[str, str], sample_bytes: int) -> tuple[float, int, str]:
    """读取有限字节测真实下载速率；直播流是无限流，必须用 read1 避免阻塞到超时。"""
    started = time.perf_counter()
    total = 0
    try:
        with _open(url, cfg, timeout, headers) as resp:
            reader = getattr(resp, "read1", None)
            while total < sample_bytes and time.perf_counter() - started < timeout:
                chunk = reader(min(65536, sample_bytes - total)) if reader else resp.read(1)
                if not chunk:
                    break
                total += len(chunk)
            content_type = resp.headers.get("Content-Type", "")
    except Exception as exc:
        return 0.0, 0, f"{type(exc).__name__}: {exc}"
    elapsed = max(time.perf_counter() - started, 1e-3)
    mbps = total * 8 / elapsed / 1_000_000
    return round(mbps, 3), total, content_type


def analyze_hls(text: str, base_url: str) -> dict:
    """解析 HLS：master 变体的 RESOLUTION 与 media 分片地址。"""
    is_hls = "#EXTM3U" in text[:200] or "#EXT-X-" in text
    result = {"is_hls": is_hls, "height": 0, "codecs": "", "variants": [], "segments": []}
    if not is_hls:
        return result
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.upper().startswith("#EXT-X-STREAM-INF:"):
            width, height = 0, 0
            match = RESOLUTION_RE.search(line)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
            codecs = CODECS_RE.search(line)
            for follower in lines[index + 1: index + 4]:
                if follower.startswith("#"):
                    continue
                result["variants"].append(
                    {
                        "url": absolute_url(base_url, follower),
                        "width": width,
                        "height": height,
                        "bandwidth": 0,
                        "codecs": codecs.group(1) if codecs else "",
                    }
                )
                break
        elif not line.startswith("#") and SEGMENT_RE.search(line):
            result["segments"].append(absolute_url(base_url, line))
    if result["variants"]:
        best = max(result["variants"], key=lambda v: (v["height"] * v["width"], v["bandwidth"]))
        result["height"] = best["height"]
        result["codecs"] = best["codecs"]
    return result


def _fetch_manifest(url: str, cfg, timeout: float, headers: dict[str, str], max_bytes: int) -> tuple[str, dict]:
    """只嗅探响应头部若干字节：直播流是无限流，不能用 read(n) 等它读满。"""
    started = time.perf_counter()
    chunks: list[bytes] = []
    total = 0
    deadline = started + min(timeout, 3.0)
    limit = min(max_bytes, 1_000_000)
    with _open(url, cfg, timeout, headers) as resp:
        reader = getattr(resp, "read1", None)
        while total < limit and time.perf_counter() < deadline:
            chunk = reader(min(65536, limit - total)) if reader else resp.read(1)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        meta = {
            "status": resp.status,
            "content_type": resp.headers.get("Content-Type", ""),
            "ttfb_ms": round((time.perf_counter() - started) * 1000, 1),
            "final_url": resp.geturl(),
        }
    return b"".join(chunks).decode("utf-8", "replace"), meta


def quick_check(stream: Stream, cfg) -> QuickResult:
    """单轮快速探测：延迟、速率、HLS 分辨率线索。绝不拉完整视频流。"""
    p, net = cfg["probe"], cfg["network"]
    timeout = float(p["read_timeout"])
    headers = _headers_for(stream, cfg)
    url = stream.url
    result = QuickResult()
    try:
        text, meta = _fetch_manifest(url, cfg, float(p["connect_timeout"]), headers, int(net["max_source_bytes"]))
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.status = meta["status"]
    result.content_type = meta["content_type"]
    result.latency_ms = meta["ttfb_ms"]
    is_playlist = (
        ".m3u8" in url.lower()
        or "mpegurl" in meta["content_type"].lower()
        or text.lstrip().startswith("#EXTM3U")
    )
    if is_playlist:
        hls = analyze_hls(text, meta["final_url"])
        result.playlist_height = hls["height"]
        result.playlist_codec = hls["codecs"]
        result.variant_urls = [v["url"] for v in sorted(hls["variants"], key=lambda v: -v["height"])[:2]]
        target_variant = result.variant_urls[0] if result.variant_urls else None
        segment = hls["segments"][0] if hls["segments"] else None
        if target_variant:
            try:
                sub_text, sub_meta = _fetch_manifest(target_variant, cfg, float(p["connect_timeout"]), headers, int(net["max_source_bytes"]))
                sub_hls = analyze_hls(sub_text, sub_meta["final_url"])
                segment = sub_hls["segments"][0] if sub_hls["segments"] else None
                if sub_hls["height"] and sub_hls["height"] > result.playlist_height:
                    result.playlist_height = sub_hls["height"]
            except Exception as exc:
                LOG.debug("变体清单不可达 %s: %s", target_variant, exc)
        if segment:
            mbps, nbytes, _ = _download_probe(segment, cfg, timeout, headers, int(p["sample_bytes"]))
            result.speed_mbps = mbps
            result.bytes_read = nbytes
            result.ok = nbytes > 0
            if not result.ok:
                result.error = "分片不可读"
        else:
            # 只有 master 清单且变体不可达，仍认为清单有效，交给 ffprobe 判定
            result.ok = bool(hls["variants"])
            result.speed_mbps = 0.0
            result.bytes_read = len(text)
            if not result.ok:
                result.error = "清单无有效分片/变体"
    else:
        # 非 HLS（TS/FLV/MP4 等）：单独做一次限速下载采样，得到真实速率
        mbps, nbytes, ctype = _download_probe(url, cfg, timeout, headers, int(p["sample_bytes"]))
        result.speed_mbps = mbps
        result.bytes_read = nbytes
        if ctype:
            result.content_type = ctype
        result.ok = nbytes > 0 and result.status < 400
        if not result.ok:
            result.error = "无可读数据"
    return result


def deep_probe(stream: Stream, cfg) -> DeepResult:
    """用 ffprobe 拉取视频流元数据，读取真实分辨率。"""
    p = cfg["probe"]
    if not p["ffprobe_enabled"] or not has_ffprobe():
        return DeepResult(ok=False, error="ffprobe 不可用或未启用")
    timeout = float(p["ffprobe_timeout"])
    cmd = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", str(int(timeout * 1_000_000)),
        "-i", stream.url,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,avg_frame_rate",
        "-of", "json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        if proc.returncode != 0:
            return DeepResult(ok=False, error=(proc.stderr or "ffprobe 退出码非 0").strip()[:160])
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return DeepResult(ok=False, error="未检测到视频流")
        info = streams[0]
        fps_raw = str(info.get("avg_frame_rate", "0/1"))
        try:
            num, den = fps_raw.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except Exception:
            fps = 0.0
        return DeepResult(
            ok=True,
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            codec=str(info.get("codec_name") or ""),
            fps=round(fps, 2),
        )
    except subprocess.TimeoutExpired:
        return DeepResult(ok=False, error="ffprobe 超时")
    except Exception as exc:
        return DeepResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:160])


def merge_rounds(rounds: Iterable[QuickResult]) -> dict:
    """多轮采样汇总：成功率、平均延迟、抖动、平均速率。"""
    items = [r for r in rounds if r is not None]
    oks = [r for r in items if r.ok]
    latencies = [r.latency_ms for r in oks]
    speeds = [r.speed_mbps for r in oks]
    jitter = 0.0
    if len(latencies) > 1:
        jitter = round(statistics.pstdev(latencies), 2)
    elif latencies:
        jitter = 0.0
    return {
        "rounds_ok": len(oks),
        "rounds_total": len(items),
        "success_rate": round(len(oks) / len(items), 3) if items else 0.0,
        "latency_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
        "jitter_ms": jitter,
        "speed_mbps": round(statistics.mean(speeds), 3) if speeds else 0.0,
        "playlist_height": max((r.playlist_height for r in oks), default=0),
        "variant_urls": list(dict.fromkeys(v for r in oks for v in r.variant_urls))[:2],
        "last_error": next((r.error for r in reversed(items) if r.error), ""),
    }
