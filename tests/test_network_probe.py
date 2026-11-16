"""本地 HTTP 集成测试：用临时服务器验证真实网络探测链路（不访问外网）。

覆盖：HTTP 拉取 M3U、HLS master RESOLUTION 解析、变体清单、分片下载测速、失效源判定。
"""

from __future__ import annotations

import http.server
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from livehd.config import Config  # noqa: E402
from livehd.models import Stream  # noqa: E402
from livehd.probe import quick_check  # noqa: E402
from livehd.util import http_get  # noqa: E402

SETTINGS = ROOT / "config" / "settings.json"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        port = self.server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        if self.path == "/playlist.m3u":
            body = (
                "#EXTM3U\n"
                '#EXTINF:-1 tvg-id="CCTV-1" group-title="央视",CCTV-1\n'
                f"{base}/master.m3u8\n"
            ).encode()
            self._send(200, body, "application/vnd.apple.mpegurl")
        elif self.path == "/master.m3u8":
            body = (
                "#EXTM3U\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.64001f"\n'
                "low/index.m3u8\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS="avc1.640028"\n'
                "high/index.m3u8\n"
            ).encode()
            self._send(200, body, "application/vnd.apple.mpegurl")
        elif self.path == "/high/index.m3u8":
            self._send(
                200,
                b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg1.ts\n#EXTINF:4.0,\nseg2.ts\n",
                "application/vnd.apple.mpegurl",
            )
        elif self.path == "/high/seg1.ts":
            self._send(200, b"\x47" * 200_000, "video/mp2t")
        else:
            self._send(404, b"not found")


class LiveServer:
    def __enter__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class NetworkProbeTests(unittest.TestCase):
    def test_real_http_hls_probe(self):
        cfg = Config(SETTINGS)
        cfg.data["probe"]["sample_bytes"] = 65536
        with LiveServer() as base:
            text, meta = http_get(
                f"{base}/playlist.m3u",
                timeout=5,
                user_agent=cfg["network"]["user_agent"],
            )
            self.assertEqual(meta["status"], 200)
            self.assertIn("/master.m3u8", text)

            stream = Stream(url=f"{base}/master.m3u8", name="CCTV-1", group="央视")
            result = quick_check(stream, cfg)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.playlist_height, 1080, "应读取 master 里最高的 RESOLUTION")
            self.assertEqual(len(result.variant_urls), 2)
            self.assertGreater(result.speed_mbps, 0, "应实测到分片下载速率")
            self.assertGreater(result.bytes_read, 0)
            self.assertLess(result.latency_ms, 2000)

            bad = quick_check(Stream(url=f"{base}/missing.m3u8"), cfg)
            self.assertFalse(bad.ok)
            self.assertIn("404", bad.error + str(bad.status))


if __name__ == "__main__":
    unittest.main(verbosity=2)
