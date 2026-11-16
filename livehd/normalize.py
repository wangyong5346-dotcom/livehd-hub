"""归一化层：频道名清洗、别名合并、分组归类、黑白名单过滤。"""

from __future__ import annotations

import re

from .models import Channel, Stream
from .util import LOG, normalize_text, strip_quality_mark, url_hash

_CCTV_RE = re.compile(r"(?i)^\s*cctv[\s\-_]*(\d{1,2})\s*$")


def canonical_name(raw: str, aliases: dict[str, str], whitelist: dict[str, Channel]) -> str:
    name = normalize_text(raw)
    name = re.sub(r"\$[^$]*$", "", name).strip()
    name = strip_quality_mark(name)
    # CCTV1 / CCTV 1 / CCTV-1 统一为 CCTV-1
    match = _CCTV_RE.match(name)
    if match:
        name = f"CCTV-{int(match.group(1))}"
    key = name.lower()
    for alias, target in aliases.items():
        if key == normalize_text(alias).lower():
            return target
    for channel_name in whitelist:
        if key == channel_name.lower():
            return channel_name
    return name


def guess_group(name: str, declared: str, groups: list[dict], whitelist: dict[str, Channel]) -> str:
    if name in whitelist and whitelist[name].group:
        return whitelist[name].group
    if declared:
        return normalize_text(declared)
    for rule in groups:
        keywords = rule.get("keywords", [])
        if any(k.lower() in name.lower() for k in keywords if k):
            return rule.get("name", "其他")
    return "其他"


def normalize(streams: list[Stream], cfg) -> tuple[dict[str, Channel], dict]:
    rules = cfg.channels
    aliases: dict[str, str] = {str(k): str(v) for k, v in rules.get("aliases", {}).items()}
    groups: list[dict] = rules.get("groups", [])
    group_order = {g["name"]: i for i, g in enumerate(groups)}
    whitelist: dict[str, Channel] = {}
    for item in rules.get("channels", []):
        cname = normalize_text(item.get("name", ""))
        if not cname:
            continue
        aliases.update({str(a): cname for a in item.get("aliases", [])})
        whitelist[cname] = Channel(name=cname, group=item.get("group", ""), logo=item.get("logo", ""), tvg_id=item.get("tvg_id", ""))
    only_whitelist = bool(rules.get("only_whitelist", False))
    # 分组白名单：按原始 group-title 关键字保留整组频道（例如源里的「港澳台频道」）。
    # 用于「只要某一地区全部频道、不便逐条枚举频道名」的场景，源更新后也能自动纳入新频道。
    allowed_groups = [str(x) for x in rules.get("allowed_groups", []) if x]
    allowed_group_name = str(rules.get("allowed_group_name", "港澳台"))

    channels: dict[str, Channel] = {}
    dropped = 0
    alias_hits = 0
    for stream in streams:
        raw = stream.name
        name = canonical_name(raw, aliases, whitelist)
        if not name:
            dropped += 1
            continue
        declared_group = stream.group or ""
        in_allowed_group = bool(allowed_groups) and any(k in declared_group for k in allowed_groups)
        if only_whitelist and name not in whitelist and not in_allowed_group:
            dropped += 1
            continue
        if name != normalize_text(raw):
            alias_hits += 1
        if in_allowed_group and name not in whitelist:
            stream.group = allowed_group_name
        group = guess_group(name, stream.group, groups, whitelist)
        stream.name = name
        stream.group = group
        if name not in channels:
            template = whitelist.get(name)
            channels[name] = Channel(
                name=name,
                group=group,
                logo=(template.logo if template else "") or stream.tvg_logo,
                tvg_id=(template.tvg_id if template else "") or stream.tvg_id,
            )
        seen = {url_hash(s.url) for s in channels[name].streams}
        if url_hash(stream.url) not in seen:
            channels[name].streams.append(stream)

    ordered = dict(
        sorted(
            channels.items(),
            key=lambda kv: (group_order.get(kv[1].group, 999), kv[0]),
        )
    )
    report = {
        "channels": len(ordered),
        "streams": sum(len(c.streams) for c in ordered.values()),
        "alias_normalized": alias_hits,
        "dropped": dropped,
        "only_whitelist": only_whitelist,
    }
    LOG.info("归一化完成：%d 个频道 / %d 条候选流（别名合并 %d，丢弃 %d）", report["channels"], report["streams"], alias_hits, dropped)
    return ordered, report
