"""离线单元测试：不联网、不需要 ffprobe。运行: python -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from livehd import collect, generate, normalize  # noqa: E402
from livehd.config import Config  # noqa: E402
from livehd.models import DeepResult, QuickResult, ScoredStream, Stream  # noqa: E402
from livehd.pipeline import Pipeline  # noqa: E402
from livehd.probe import analyze_hls  # noqa: E402
from livehd.score import compute_score, hard_gate, select_top  # noqa: E402
from livehd.state import QualityState  # noqa: E402
from main import SELFTEST_CHANNELS, fake_deep, fake_quick  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SETTINGS = ROOT / "config" / "settings.json"


def isolated_config() -> Config:
    """固定频道规则，与使用者实际配置的白名单解耦。"""
    cfg = Config(SETTINGS)
    cfg.channels = copy.deepcopy(SELFTEST_CHANNELS)
    return cfg


class ParseTests(unittest.TestCase):
    def test_parse_m3u(self):
        text = (FIXTURES / "sample.m3u").read_text(encoding="utf-8")
        streams = collect.parse_m3u(text, "https://base.example/list.m3u", "unit")
        self.assertEqual(len(streams), 7)
        self.assertEqual(streams[0].tvg_id, "CCTV-1")
        self.assertEqual(streams[0].group, "央视")
        self.assertTrue(streams[0].url.startswith("https://"))

    def test_parse_txt_genre_and_backup(self):
        text = (FIXTURES / "sample.txt").read_text(encoding="utf-8")
        streams = collect.parse_txt(text, "https://base.example/list.txt", "unit")
        self.assertEqual(len(streams), 3)
        self.assertEqual(streams[0].group, "央视")
        self.assertEqual(streams[-1].group, "卫视")

    def test_hls_master_resolution(self):
        text = (FIXTURES / "master.m3u8").read_text(encoding="utf-8")
        info = analyze_hls(text, "https://cdn.example.com/master.m3u8")
        self.assertTrue(info["is_hls"])
        self.assertEqual(info["height"], 1080)
        self.assertEqual(len(info["variants"]), 2)
        self.assertTrue(info["variants"][0]["url"].startswith("https://"))

    def test_hls_media_segments(self):
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg1.ts\n#EXTINF:4.0,\nseg2.ts\n"
        info = analyze_hls(text, "https://cdn.example.com/low/index.m3u8")
        self.assertEqual(len(info["segments"]), 2)
        self.assertEqual(info["segments"][0], "https://cdn.example.com/low/seg1.ts")


class NormalizeTests(unittest.TestCase):
    def test_alias_merge_and_group(self):
        cfg = isolated_config()
        text = (FIXTURES / "sample.m3u").read_text(encoding="utf-8")
        streams = collect.parse_m3u(text, "https://base.example/list.m3u", "unit")
        channels, report = normalize.normalize(streams, cfg)
        self.assertIn("CCTV-1", channels)
        self.assertEqual(len(channels["CCTV-1"].streams), 2, "别名应把 CCTV1 HD 合并到 CCTV-1")
        self.assertEqual(channels["CCTV-5"].group, "体育")
        self.assertGreaterEqual(report["alias_normalized"], 1)

    def test_whitelist_only_mode(self):
        cfg = isolated_config()
        cfg.channels["only_whitelist"] = True
        streams = collect.parse_m3u((FIXTURES / "sample.m3u").read_text(encoding="utf-8"), "https://b.example/x.m3u", "u")
        channels, _ = normalize.normalize(streams, cfg)
        self.assertNotIn("测试失效", channels)

    def test_allowed_group_keeps_whole_group(self):
        cfg = isolated_config()
        cfg.channels["only_whitelist"] = True
        cfg.channels["allowed_groups"] = ["港澳台"]
        cfg.channels["allowed_group_name"] = "港澳台"
        text = (
            '#EXTM3U\n'
            '#EXTINF:-1 group-title="🔮港澳台频道",翡翠台\nhttps://a.example/1.m3u8\n'
            '#EXTINF:-1 group-title="🔮港澳台频道[翻]",凤凰中文\nhttps://a.example/2.m3u8\n'
            '#EXTINF:-1 group-title="🇨🇳央卫视频道[1]",北京卫视\nhttps://a.example/3.m3u8\n'
            '#EXTINF:-1 group-title="🇨🇳央卫视频道[1]",CCTV1综合\nhttps://a.example/4.m3u8\n'
        )
        streams = collect.parse_m3u(text, "https://b.example/x.m3u", "u")
        channels, report = normalize.normalize(streams, cfg)
        self.assertIn("翡翠台", channels)
        self.assertIn("凤凰中文", channels)
        self.assertEqual(channels["翡翠台"].group, "港澳台")
        self.assertIn("CCTV-1", channels, "别名应命中 CCTV 白名单")
        self.assertNotIn("北京卫视", channels, "非白名单且非允许分组的频道应被丢弃")
        self.assertEqual(report["dropped"], 1)


class ScoreTests(unittest.TestCase):
    def _item(self, height=1080, latency=120, speed=5.0, rate=1.0, jitter=10, width=1920):
        return ScoredStream(
            stream=Stream(url="https://h.example.com/live.m3u8", name="CCTV-1", group="央视"),
            height=height, width=width, latency_ms=latency, speed_mbps=speed,
            success_rate=rate, jitter_ms=jitter, history_score=0.8, rounds_ok=2, rounds_total=2,
            resolution_source="ffprobe",
        )

    def test_720p_rejected_1080p_passed(self):
        cfg = Config(SETTINGS)
        low = self._item(height=720, width=1280)
        high = self._item()
        self.assertFalse(hard_gate(low, cfg)[0])
        ok, reasons = hard_gate(high, cfg)
        self.assertTrue(ok, reasons)
        self.assertGreater(compute_score(high, cfg), 0)

    def test_latency_and_speed_gate(self):
        cfg = Config(SETTINGS)
        q = cfg["quality"]
        # 用相对阈值构造越界样本，避免调整实际配置后测试失效
        self.assertFalse(hard_gate(self._item(latency=float(q["max_latency_ms"]) + 1000), cfg)[0])
        self.assertFalse(hard_gate(self._item(speed=max(0.0, float(q["min_speed_mbps"]) / 3)), cfg)[0])
        self.assertFalse(hard_gate(self._item(rate=0.5), cfg)[0])

    def test_select_top_limits_per_host(self):
        cfg = Config(SETTINGS)
        items = []
        for i in range(5):
            s = self._item()
            s.stream.url = f"https://same.example.com/{i}.m3u8"
            s.score = 100 - i
            items.append(s)
        chosen = select_top(items, cfg)
        self.assertEqual(len(chosen), 1, "同一主机默认只保留 1 条")


class StateTests(unittest.TestCase):
    def test_ewma_and_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = QualityState(Path(tmp) / "q.json", quarantine_failures=3, quarantine_hours=6)
            url = "https://x.example.com/a.m3u8"
            self.assertAlmostEqual(state.history_score(url), 0.5)
            for _ in range(3):
                state.update(url, False)
            self.assertTrue(state.is_quarantined(url))
            state.update(url, True, latency_ms=100, speed_mbps=4, height=1080)
            self.assertFalse(state.is_quarantined(url))
            state.save()
            self.assertTrue((Path(tmp) / "q.json").exists())


class PipelineTests(unittest.TestCase):
    def test_offline_end_to_end(self):
        cfg = isolated_config()
        cfg.sources["sources"] = [{
            "name": "unit-local", "type": "playlist",
            "url": str(FIXTURES / "sample.m3u"), "format": "m3u", "enabled": True,
        }]
        cfg.data["network"]["allow_local_files"] = True
        cfg.data["probe"]["rounds"] = 2
        cfg.data["probe"]["round_interval"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            cfg.data["output_dir"] = tmp
            state = QualityState(Path(tmp) / "quality.json")
            report = Pipeline(cfg, state, quick_fn=fake_quick, deep_fn=fake_deep).run()
            m3u = (Path(tmp) / "live.m3u").read_text(encoding="utf-8")
            self.assertGreater(report["channels_with_result"], 0)
            self.assertGreater(report["final_streams"], 0)
            self.assertIn("#EXTM3U", m3u)
            self.assertNotIn("bad.m3u8", m3u, "失效源必须被剔除")
            self.assertNotIn("sd.m3u8", m3u, "720p 源必须被 1080p 门槛剔除")
            self.assertIn("hunan_4k", m3u, "4K 源应保留")
            self.assertIn("4K/2160p+", report["resolution_distribution"])
            for name in ("live.m3u", "live.txt", "channels.json", "status.json", "index.html"):
                self.assertTrue((Path(tmp) / name).exists(), name)
            status = json.loads((Path(tmp) / "status.json").read_text(encoding="utf-8"))
            self.assertTrue(status["subscription_links"]["m3u_raw"].endswith("/docs/live.m3u"),
                            "试运行输出临时目录时，订阅链接仍应指向发布目录 docs")

    def test_fixed_links(self):
        os.environ["GITHUB_REPOSITORY"] = "alice/livehd"
        os.environ["GITHUB_REF_NAME"] = "main"
        links = generate.fixed_links("docs")
        self.assertEqual(links["m3u_raw"], "https://raw.githubusercontent.com/alice/livehd/main/docs/live.m3u")
        self.assertIn("cdn.jsdelivr.net/gh/alice/livehd@main/docs/live.m3u", links["m3u_jsdelivr"])
        self.assertIn("alice.github.io", links["m3u_pages"])


class SourceAdapterTests(unittest.TestCase):
    """公开索引分表适配：名称标记清洗 + 源级默认分组。"""

    def test_quality_and_status_marks_stripped(self):
        from livehd.util import strip_quality_mark
        self.assertEqual(strip_quality_mark("CCTV-13 (1080p) [Geo-blocked]"), "CCTV-13")
        self.assertEqual(strip_quality_mark("HOY TV (1080p) [Not 24/7]"), "HOY TV")
        self.assertEqual(normalize.canonical_name("CCTV-1 (1080p)", {}, {}), "CCTV-1")

    def test_default_group_allows_group_whitelist(self):
        cfg = isolated_config()
        cfg.channels["only_whitelist"] = True
        cfg.channels["allowed_groups"] = ["港澳台"]
        cfg.channels["allowed_group_name"] = "港澳台"
        cfg.channels["channels"] = []  # 白名单为空，只有分组白名单能放行
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "region.m3u"
            path.write_text(
                "#EXTM3U\n#EXTINF:-1,RTHK TV 31 (1080p) [Geo-blocked]\n"
                "https://example.com/rthk31.m3u8\n",
                encoding="utf-8",
            )
            cfg.sources["sources"] = [{
                "name": "region", "type": "playlist", "url": str(path),
                "format": "m3u", "default_group": "港澳台", "enabled": True,
            }]
            cfg.data["network"]["allow_local_files"] = True
            streams, _ = collect.collect(cfg)
            self.assertEqual(len(streams), 1)
            self.assertEqual(streams[0].group, "港澳台")
            channels, _ = normalize.normalize(streams, cfg)
            self.assertIn("RTHK TV 31", channels)
            self.assertEqual(channels["RTHK TV 31"].group, "港澳台")


if __name__ == "__main__":
    unittest.main(verbosity=2)
