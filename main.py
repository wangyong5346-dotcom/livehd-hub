#!/usr/bin/env python3
"""LiveHD Hub CLI 入口。

用法：
  python main.py                          # 按 config/settings.json 运行完整流水线
  python main.py --self-test              # 离线自测（不联网、不需要 ffprobe）
  python main.py --local-sources a.m3u b.txt --workers 16 --rounds 2
  python main.py --no-ffprobe --allow-unverified --min-height 720
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from livehd.config import Config  # noqa: E402
from livehd.models import DeepResult, QuickResult, Stream  # noqa: E402
from livehd.pipeline import Pipeline  # noqa: E402
from livehd.state import QualityState  # noqa: E402
from livehd.util import LOG, setup_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LiveHD Hub - 高清直播源聚合质检与订阅生成")
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.json"), help="主配置文件路径")
    parser.add_argument("--output", help="覆盖输出目录")
    parser.add_argument("--workers", type=int, help="快速探测并发数")
    parser.add_argument("--rounds", type=int, help="每个源的采样轮次")
    parser.add_argument("--min-height", type=int, help="最低真实分辨率（像素高度）")
    parser.add_argument("--top-n", type=int, help="每频道保留的源数量")
    parser.add_argument("--max-candidates", type=int, help="每频道进入质检的候选上限")
    parser.add_argument("--time-budget", type=int, help="全局质检时间预算（秒）")
    parser.add_argument("--tvg-url", help="M3U 里的 EPG 节目单地址")
    parser.add_argument("--no-ffprobe", action="store_true", help="禁用 ffprobe 复核")
    parser.add_argument("--allow-unverified", action="store_true", help="允许未复核分辨率的源（不推荐）")
    parser.add_argument("--local-sources", nargs="+", help="用本地 m3u/txt 文件替代配置里的源清单")
    parser.add_argument("--dry-run", action="store_true", help="试运行，结果写入 .tmp/dryrun 而不污染 docs")
    parser.add_argument("--self-test", action="store_true", help="离线自测：用内置样例 + 模拟探测跑通全链路")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def fake_quick(stream: Stream, cfg) -> QuickResult:
    """自测用：模拟网络探测结果。"""
    if "bad" in stream.url:
        return QuickResult(ok=False, error="模拟失效源", status=404)
    height = 2160 if "4k" in stream.url else 1080
    return QuickResult(
        ok=True,
        latency_ms=95.0 + (len(stream.url) % 40),
        speed_mbps=5.2,
        status=200,
        content_type="application/vnd.apple.mpegurl",
        playlist_height=height,
        bytes_read=131072,
    )


def fake_deep(stream: Stream, cfg) -> DeepResult:
    """自测用：模拟 ffprobe 结果，其中 sd 源真实分辨率只有 720p。"""
    if "sd" in stream.url:
        return DeepResult(ok=True, width=1280, height=720, codec="h264", fps=25)
    if "4k" in stream.url:
        return DeepResult(ok=True, width=3840, height=2160, codec="h265", fps=50)
    return DeepResult(ok=True, width=1920, height=1080, codec="h264", fps=25)


def run_selftest(cfg_path: str) -> dict:
    setup_logging(False)
    cfg = Config(cfg_path)
    fixture = ROOT / "tests" / "fixtures" / "sample.m3u"
    cfg.sources["sources"] = [
        {
            "name": "selftest-local",
            "type": "playlist",
            "url": str(fixture),
            "format": "m3u",
            "enabled": True,
        }
    ]
    cfg.data["network"]["allow_local_files"] = True
    cfg.data["probe"]["rounds"] = 2
    cfg.data["probe"]["round_interval"] = 0
    cfg.data["probe"]["time_budget_seconds"] = 60
    cfg.data["output_dir"] = ".tmp/selftest"
    state = QualityState(cfg.resolve(".tmp/selftest/quality.json"))
    report = Pipeline(cfg, state, quick_fn=fake_quick, deep_fn=fake_deep).run()
    out = cfg.resolve(".tmp/selftest")
    for filename in ("live.m3u", "live.txt", "channels.json", "status.json", "index.html"):
        assert (out / filename).exists(), f"缺少输出文件: {filename}"
    assert report["channels_with_result"] > 0, "自测没有产出任何频道"
    assert report["final_streams"] > 0, "自测没有产出任何源"
    assert not any("sd" in line for line in (out / "live.m3u").read_text(encoding="utf-8").splitlines()), "720p 源未被 1080p 门槛过滤"
    assert not any("bad" in line for line in (out / "live.m3u").read_text(encoding="utf-8").splitlines()), "失效源未被过滤"
    LOG.info("离线自测通过：%d 频道 / %d 条高清源", report["channels_with_result"], report["final_streams"])
    return report


def main() -> int:
    args = build_parser().parse_args()
    setup_logging(args.verbose)
    if args.self_test:
        report = run_selftest(args.config)
        print(json.dumps({"self_test": "passed", "summary": {
            k: report[k] for k in ("channels_with_result", "final_streams", "candidates", "deep_checked", "duration_seconds")
        }}, ensure_ascii=False, indent=2))
        return 0

    overrides: dict = {}
    if args.output:
        overrides["output_dir"] = args.output
    if args.dry_run and not args.output:
        overrides["output_dir"] = ".tmp/dryrun"
    cfg = Config(args.config, overrides or None)
    cfg.apply_cli(args)
    state = QualityState(
        cfg.resolve(cfg["state_file"]),
        int(cfg["selection"]["quarantine_failures"]),
        float(cfg["selection"]["quarantine_hours"]),
    )
    report = Pipeline(cfg, state).run()
    print(json.dumps({
        "generated_at": report["generated_at"],
        "channels": report["channels_with_result"],
        "streams": report["final_streams"],
        "candidates": report["candidates"],
        "quick_passed": report["quick_passed"],
        "ffprobe_checked": report["deep_checked"],
        "duration_seconds": report["duration_seconds"],
        "output_files": report["output_files"],
        "reject_reasons": report["reject_reasons"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
