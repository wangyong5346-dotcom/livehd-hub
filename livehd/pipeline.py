"""编排层：采集 → 归一化 → 候选裁剪 → 快速探测(多轮) → ffprobe 复核 → 评分选优 → 生成订阅。"""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import collect as collect_mod
from . import generate as generate_mod
from . import normalize as normalize_mod
from . import probe as probe_mod
from . import score as score_mod
from .models import DeepResult, QuickResult, ScoredStream, Stream
from .state import QualityState
from .util import LOG, now_iso, url_hash


def map_with_budget(items: list, fn, workers: int, deadline: float, label: str) -> tuple[dict, bool]:
    """并发执行，受全局时间预算约束；超时后取消未开始任务，不阻塞整个工作流。"""
    if not items:
        return {}, False
    executor = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    results: dict[int, object] = {}
    exhausted = False
    try:
        futures = {executor.submit(fn, item): idx for idx, item in enumerate(items)}
        try:
            for future in as_completed(futures, timeout=max(1.0, deadline - time.time())):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    LOG.debug("%s 单条失败: %s", label, exc)
                    results[idx] = None
        except TimeoutError:
            exhausted = True
            LOG.warning("%s 达到时间预算，跳过剩余 %d 项", label, len(items) - len(results))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results, exhausted


class Pipeline:
    def __init__(self, cfg, state: QualityState | None = None, quick_fn=None, deep_fn=None):
        self.cfg = cfg
        self.state = state or QualityState(
            cfg.resolve(cfg["state_file"]),
            int(cfg["selection"]["quarantine_failures"]),
            float(cfg["selection"]["quarantine_hours"]),
        )
        self.quick_fn = quick_fn or probe_mod.quick_check
        self.deep_fn = deep_fn or probe_mod.deep_probe

    def _candidates(self, channels: dict) -> tuple[list[Stream], int]:
        p = self.cfg["probe"]
        limit = int(p["max_candidates_per_channel"])
        candidates: list[Stream] = []
        quarantined = 0
        for channel in channels.values():
            active = [s for s in channel.streams if not self.state.is_quarantined(s.url)]
            quarantined += len(channel.streams) - len(active)
            if not active:  # 整组都被隔离时强制复活少量源，避免频道永久消失
                active = list(channel.streams)
                quarantined -= min(len(active), limit)
            active.sort(key=lambda s: -self.state.history_score(s.url))
            candidates.extend(active[:limit])
        # 全局去重（同一 URL 出现在多个频道时只测一次）
        seen: set[str] = set()
        unique = []
        for stream in candidates:
            if url_hash(stream.url) not in seen:
                seen.add(url_hash(stream.url))
                unique.append(stream)
        LOG.info("候选裁剪：%d 条进入快速探测（隔离跳过 %d）", len(unique), max(0, quarantined))
        return unique, max(0, quarantined)

    def _quick_stage(self, candidates: list[Stream], deadline: float) -> tuple[dict[str, list[QuickResult]], bool]:
        p = self.cfg["probe"]
        rounds = max(1, int(p["rounds"]))
        interval = float(p.get("round_interval", 1.0))
        rounds_map: dict[str, list[QuickResult]] = {s.url: [] for s in candidates}
        active = list(candidates)
        exhausted = False
        for round_no in range(1, rounds + 1):
            if time.time() >= deadline or not active:
                break
            LOG.info("快速探测 第 %d/%d 轮：%d 条", round_no, rounds, len(active))
            mapped, timed_out = map_with_budget(
                active,
                lambda s: self.quick_fn(s, self.cfg),
                int(p["quick_workers"]),
                deadline,
                f"quick-r{round_no}",
            )
            exhausted = exhausted or timed_out
            next_active: list[Stream] = []
            for idx, stream in enumerate(active):
                result = mapped.get(idx)
                if not isinstance(result, QuickResult):
                    result = QuickResult(ok=False, error="超时或未返回结果")
                rounds_map[stream.url].append(result)
                if result.ok:
                    next_active.append(stream)
            active = next_active
            if round_no < rounds and interval > 0 and active:
                time.sleep(min(interval, max(0.0, deadline - time.time())))
        return rounds_map, exhausted

    def run(self) -> dict:
        started = time.time()
        cfg = self.cfg
        q, p, sel = cfg["quality"], cfg["probe"], cfg["selection"]
        deadline = started + float(p["time_budget_seconds"])
        min_height = int(q["min_height"])

        streams, collect_report = collect_mod.collect(cfg)
        channels, norm_report = normalize_mod.normalize(streams, cfg)
        candidates, quarantined = self._candidates(channels)

        rounds_map, budget_exhausted = self._quick_stage(candidates, deadline)
        merged_by_url = {url: probe_mod.merge_rounds(rounds) for url, rounds in rounds_map.items()}

        # 用快速阶段结果构建初筛对象，按频道分组
        prefiltered: dict[str, list[ScoredStream]] = {}
        reject: Counter = Counter()
        stream_index = {s.url: s for s in candidates}
        for url, merged in merged_by_url.items():
            stream = stream_index[url]
            hint = merged["playlist_height"] or stream.claimed_height
            item = ScoredStream(
                stream=stream,
                success_rate=merged["success_rate"],
                latency_ms=merged["latency_ms"],
                jitter_ms=merged["jitter_ms"],
                speed_mbps=merged["speed_mbps"],
                height=hint,
                width=0,
                resolution_source="playlist" if merged["playlist_height"] else ("claimed" if hint else "unknown"),
                history_score=self.state.history_score(url),
                rounds_ok=merged["rounds_ok"],
                rounds_total=merged["rounds_total"],
            )
            if merged["rounds_ok"] == 0:
                reject["全部轮次不可达"] += 1
                continue
            if q.get("trust_playlist_resolution") and 0 < hint < min_height:
                reject[f"清单标称分辨率低于 {min_height}p"] += 1
                continue
            if item.latency_ms > float(q["max_latency_ms"]):
                reject["响应延迟超限"] += 1
                continue
            if item.speed_mbps < float(q["min_speed_mbps"]):
                reject["下载速率不足"] += 1
                continue
            if item.success_rate < float(q["min_success_rate"]):
                reject["多轮成功率不足"] += 1
                continue
            prefiltered.setdefault(stream.name, []).append(item)

        quick_passed = sum(len(v) for v in prefiltered.values())
        LOG.info("快速探测通过 %d 条（拒绝：%s）", quick_passed, dict(reject))

        # Stage B：每频道取 Top-K 做 ffprobe 真实分辨率复核
        deep_targets: list[Stream] = []
        for items in prefiltered.values():
            ranked = sorted(
                items,
                key=lambda x: (-(x.height >= min_height) * 100 - x.history_score * 20 + x.latency_ms / 100),
            )
            deep_targets.extend(i.stream for i in ranked[: max(1, int(p["ffprobe_top_k"]))])
        deep_map: dict[str, DeepResult] = {}
        if p.get("ffprobe_enabled") and time.time() < deadline and deep_targets:
            LOG.info("ffprobe 复核：%d 条（并发 %d）", len(deep_targets), p["deep_workers"])
            mapped, timed_out = map_with_budget(
                deep_targets,
                lambda s: self.deep_fn(s, cfg),
                int(p["deep_workers"]),
                deadline,
                "deep",
            )
            budget_exhausted = budget_exhausted or timed_out
            for idx, stream in enumerate(deep_targets):
                result = mapped.get(idx)
                deep_map[stream.url] = result if isinstance(result, DeepResult) else DeepResult(ok=False, error="超时")
        else:
            LOG.info("ffprobe 阶段跳过（未启用 / 无 ffprobe / 时间预算耗尽）")

        results: dict[str, list[ScoredStream]] = {}
        final_height: dict[str, int] = {}
        for channel_name, items in prefiltered.items():
            accepted: list[ScoredStream] = []
            for item in items:
                deep = deep_map.get(item.stream.url)
                if deep and deep.ok and deep.height > 0:
                    item.height = deep.height
                    item.width = deep.width
                    item.codec = deep.codec or item.codec
                    item.resolution_source = "ffprobe"
                elif q.get("allow_unverified_resolution") and item.height >= min_height:
                    item.resolution_source = item.resolution_source if item.resolution_source != "unknown" else "claimed"
                else:
                    reject["ffprobe 未确认真实分辨率"] += 1
                    continue
                passed, reasons = score_mod.hard_gate(item, cfg)
                if not passed:
                    item.reasons.extend(reasons)
                    for reason in reasons:
                        reject[reason] += 1
                    continue
                item.score = score_mod.compute_score(item, cfg)
                accepted.append(item)
                final_height[item.stream.url] = item.height
            chosen = score_mod.select_top(accepted, cfg)
            if chosen:
                results[channel_name] = chosen

        # 更新历史状态（所有被探测过的源）
        for url, merged in merged_by_url.items():
            self.state.update(
                url,
                ok=merged["rounds_ok"] > 0,
                latency_ms=merged["latency_ms"],
                speed_mbps=merged["speed_mbps"],
                height=final_height.get(url, 0),
            )
        self.state.prune({url_hash(s.url) for s in candidates})
        self.state.save()

        dist = Counter()
        for items in results.values():
            for item in items:
                dist["4K/2160p+" if item.height >= 2160 else "1080p" if item.height >= 1080 else "其他"] += 1
        duration = round(time.time() - started, 1)
        report = {
            "generated_at": now_iso(cfg["output"]["timezone"]),
            "title": cfg["output"].get("title", "LiveHD Hub"),
            "duration_seconds": duration,
            "min_height": min_height,
            "max_latency_ms": q["max_latency_ms"],
            "collect": collect_report,
            "normalize": norm_report,
            "candidates": len(candidates),
            "quarantined_skipped": quarantined,
            "quick_passed": quick_passed,
            "deep_checked": len(deep_map),
            "final_streams": sum(len(v) for v in results.values()),
            "channels_with_result": len(results),
            "resolution_distribution": dict(dist),
            "reject_reasons": dict(reject),
            "ffprobe_available": probe_mod.has_ffprobe(),
            "ffprobe_enabled": bool(p.get("ffprobe_enabled")),
            "budget_exhausted": budget_exhausted,
        }
        output_dir = cfg.resolve(cfg["output_dir"])
        files = generate_mod.write_outputs(results, report, cfg, output_dir)
        report["output_files"] = [str(Path(f).name) for f in files]
        LOG.info(
            "完成：%d 频道 / %d 条高清源，耗时 %.1fs，输出目录 %s",
            len(results), report["final_streams"], duration, output_dir,
        )
        return report
