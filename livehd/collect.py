"""采集层：HTTP/TXT/M3U/GitHub 目录发现，统一解析为 Stream 列表。

设计要点：
1. 源清单完全由用户配置（config/sources.json），程序不内置任何播放地址；
2. 订阅源支持一层递归展开，GitHub 目录源通过 REST API 读取（可在 Actions 里用 GITHUB_TOKEN 提额）；
3. 所有网络读取都有超时、大小上限与协议白名单。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import Stream
from .util import (
    LOG,
    absolute_url,
    claimed_height,
    host_of,
    http_get,
    is_fetchable,
    normalize_text,
    parse_attrs,
    url_hash,
)

EXTINF_RE = re.compile(r"^#EXTINF\s*:\s*(?P<head>[^,]*)\s*,\s*(?P<name>.*)$", re.I)
RESOLUTION_RE = re.compile(r"RESOLUTION=(\d{2,5})[xX](\d{2,5})")
CODECS_RE = re.compile(r'CODECS="([^"]*)"')
PLAYLIST_EXT = (".m3u", ".m3u8", ".txt")


def _looks_like_media_url(text: str) -> bool:
    t = text.strip()
    if not t or t.startswith("#"):
        return False
    if t.startswith(("http://", "https://", "//", "/", "./", "../")):
        return True
    return bool(re.search(r"\.(m3u8?|ts|mp4|flv|m4s)(\?|$)", t, re.I))


def parse_m3u(text: str, base_url: str, source_name: str = "") -> list[Stream]:
    """解析扩展 M3U，兼容 tvg-* / group-title / #EXTGRP / Kodi 属性。"""
    streams: list[Stream] = []
    pending: dict | None = None
    group_override = ""
    header_hints: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTM3U"):
            continue
        if upper.startswith("#EXTINF"):
            match = EXTINF_RE.match(line)
            if not match:
                continue
            attrs = parse_attrs(match.group("head"))
            name = normalize_text(match.group("name"))
            if "$" in name:
                name = normalize_text(name.split("$", 1)[0])
            pending = {
                "name": name,
                "group": attrs.get("group-title", "") or group_override,
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "tvg_logo": attrs.get("tvg-logo", ""),
                "attrs": attrs,
            }
            continue
        if upper.startswith("#EXTGRP:"):
            group_override = normalize_text(line.split(":", 1)[1])
            continue
        if upper.startswith("#KODIPROP") or upper.startswith("#EXTVLCOPT"):
            low = line.lower()
            if "http-referrer" in low or "referer" in low:
                header_hints["Referer"] = line.split("=", 1)[-1].strip()
            elif "user-agent" in low:
                header_hints["User-Agent"] = line.split("=", 1)[-1].strip()
            continue
        if line.startswith("#"):
            continue
        if pending is None or not _looks_like_media_url(line):
            continue
        url = absolute_url(base_url, line)
        if not is_fetchable(url, allow_local=False):
            continue
        attrs = dict(pending["attrs"])
        if header_hints:
            attrs["_headers"] = dict(header_hints)
        streams.append(
            Stream(
                url=url,
                name=pending["name"] or pending["tvg_name"],
                group=pending["group"],
                tvg_id=pending["tvg_id"],
                tvg_name=pending["tvg_name"],
                tvg_logo=pending["tvg_logo"],
                source_name=source_name,
                claimed_height=claimed_height(f"{pending['name']} {json.dumps(attrs, ensure_ascii=False)}"),
                attrs=attrs,
            )
        )
        pending = None
        group_override = ""
        header_hints = {}
    return streams


def parse_txt(text: str, base_url: str, source_name: str = "") -> list[Stream]:
    """解析 TVBox 风格 TXT：分组行 `央视,#genre#`，数据行 `频道名,地址`。"""
    streams: list[Stream] = []
    group = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") and "#genre#" not in line:
            continue
        if "," not in line:
            continue
        name_part, _, url_part = line.partition(",")
        name = normalize_text(name_part.split("$", 1)[0])
        if "#genre#" in url_part:
            group = name
            continue
        # 一个频道允许多个备用地址，用 # 分隔
        for candidate in url_part.split("#"):
            candidate = candidate.strip()
            if "$" in candidate:
                candidate = candidate.split("$", 1)[0].strip()
            if not _looks_like_media_url(candidate):
                continue
            url = absolute_url(base_url, candidate)
            if not is_fetchable(url, allow_local=False):
                continue
            streams.append(
                Stream(
                    url=url,
                    name=name,
                    group=group,
                    source_name=source_name,
                    claimed_height=claimed_height(name),
                )
            )
    return streams


def parse_playlist(text: str, base_url: str, source_name: str, fmt: str = "auto") -> list[Stream]:
    chosen = fmt.lower()
    if chosen == "auto":
        chosen = "m3u" if "#EXTM3U" in text[:400].upper() or "#EXTINF" in text.upper() else "txt"
    if chosen == "m3u":
        return parse_m3u(text, base_url, source_name)
    return parse_txt(text, base_url, source_name)


def _github_api(url: str, cfg) -> object:
    net = cfg["network"]
    headers = {
        "User-Agent": net["user_agent"],
        "Accept": "application/vnd.github+json",
    }
    token = cfg.token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_github_dir(source: dict, cfg) -> list[tuple[str, str]]:
    """返回 (虚拟 URL, 内容文本) 列表；用 GitHub Contents API 避免 raw 域名访问问题。"""
    net = cfg["network"]
    api = net["github_api"].rstrip("/")
    repo = (source.get("repo") or "").strip("/")
    path = (source.get("path") or "").strip("/")
    max_files = int(source.get("max_files", 12))
    if "/" not in repo:
        raise ValueError(f"github_dir 需要 owner/repo 格式: {repo}")
    items = _github_api(f"{api}/repos/{repo}/contents/{path}", cfg)
    if isinstance(items, dict):
        items = [items]
    files: list[tuple[str, str]] = []
    for item in items:
        if len(files) >= max_files:
            break
        if not isinstance(item, dict):
            continue
        if item.get("type") == "file" and item.get("name", "").lower().endswith(PLAYLIST_EXT):
            content = item.get("content")
            if content:
                text = base64.b64decode(content).decode("utf-8", "replace")
            else:
                text, _ = http_get(
                    item.get("download_url", ""),
                    timeout=net.get("read_timeout", 8),
                    max_bytes=net["max_source_bytes"],
                    user_agent=net["user_agent"],
                )
            files.append((item.get("html_url") or item.get("url", ""), text))
    return files


def _fetch_source(source: dict, cfg) -> list[tuple[str, str, str, str]]:
    """返回 [(来源名, 内容, 基准 URL, 默认分组)]，一个订阅源可能展开成多个播放列表。

    default_group 用于给「没有 group-title 的按地区分表」补上分组标签，
    例如 iptv-org 的 streams/hk.m3u，使频道白名单能按地区整表保留。
    """
    net = cfg["network"]
    url = (source.get("url") or "").strip()
    name = source.get("name") or url
    fmt = source.get("format", "auto")
    kind = source.get("type", "playlist")
    default_group = str(source.get("default_group", "") or "")

    if kind == "github_dir":
        return [(f"{name}/{u.rsplit('/', 1)[-1]}", text, u, default_group) for u, text in discover_github_dir(source, cfg)]

    if not url or not is_fetchable(url, allow_local=net["allow_local_files"]):
        LOG.warning("跳过无效或未启用的源: %s", name)
        return []

    timeout = cfg["probe"]["read_timeout"]
    text, _ = http_get(
        url,
        timeout=timeout,
        max_bytes=net["max_source_bytes"],
        user_agent=net["user_agent"],
        headers=source.get("headers"),
        allow_local=net["allow_local_files"],
    )
    if kind != "subscription":
        return [(name, text, url, default_group)]

    # 订阅源：每行是一个新的播放列表地址
    child_urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "#genre#" in line:
            continue
        candidate = line.split(",", 1)[-1].strip() if "," in line else line
        candidate = candidate.split("#", 1)[0].strip()
        if is_fetchable(candidate, allow_local=False):
            child_urls.append(candidate)
    child_urls = list(dict.fromkeys(child_urls))[:40]
    results: list[tuple[str, str, str]] = []

    def fetch_child(child: str):
        body, _ = http_get(
            child,
            timeout=timeout,
            max_bytes=net["max_source_bytes"],
            user_agent=net["user_agent"],
            headers=source.get("headers"),
        )
        return f"{name} -> {urllib.parse.urlparse(child).netloc}", body, child, default_group

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(child_urls)))) as pool:
        futures = {pool.submit(fetch_child, c): c for c in child_urls}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                LOG.warning("订阅子源失败 %s: %s", futures[future], exc)
    return results


def _is_blacklisted(stream: Stream, blacklist: dict) -> bool:
    url_lower = stream.url.lower()
    name_lower = stream.name.lower()
    host = host_of(stream.url)
    if any(k.strip().lower() in url_lower for k in blacklist.get("url_keywords", []) if k.strip()):
        return True
    if any(k.strip().lower() in host for k in blacklist.get("domains", []) if k.strip()):
        return True
    if any(k.strip().lower() in name_lower for k in blacklist.get("channel_keywords", []) if k.strip()):
        return True
    return False


def collect(cfg) -> tuple[list[Stream], dict]:
    """并发采集全部已启用源，返回 (去重后的 Stream 列表, 统计报告)。"""
    sources = [s for s in cfg.sources.get("sources", []) if s.get("enabled") and (s.get("url") or s.get("repo"))]
    blacklist = cfg.sources.get("blacklist", {})
    payloads: list[tuple[str, str, str]] = []
    errors: list[str] = []
    LOG.info("开始采集：%d 个已启用源", len(sources))

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(sources)))) as pool:
        futures = {pool.submit(_fetch_source, s, cfg): s for s in sources}
        for future in as_completed(futures):
            src = futures[future]
            try:
                payloads.extend(future.result())
            except Exception as exc:
                msg = f"{src.get('name')}: {type(exc).__name__}: {exc}"
                LOG.warning("采集失败 %s", msg)
                errors.append(msg)

    seen: set[str] = set()
    streams: list[Stream] = []
    for name, text, base, default_group in payloads:
        try:
            parsed = parse_playlist(text, base, name)
        except Exception as exc:
            errors.append(f"{name}: 解析失败 {exc}")
            continue
        for stream in parsed:
            if default_group and not stream.group:
                stream.group = default_group
            key = url_hash(stream.url)
            if key in seen or _is_blacklisted(stream, blacklist):
                continue
            seen.add(key)
            streams.append(stream)

    report = {
        "enabled_sources": len(sources),
        "fetched_playlists": len(payloads),
        "raw_streams": len(streams),
        "errors": errors[:30],
    }
    LOG.info("采集完成：%d 个播放列表，%d 条去重后的候选流，%d 个源失败", len(payloads), len(streams), len(errors))
    return streams, report
