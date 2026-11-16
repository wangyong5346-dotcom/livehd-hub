"""通用工具：日志、时间、URL、文本归一化、受限 HTTP 读取。"""

from __future__ import annotations

import gzip
import hashlib
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger("livehd")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def now_iso(timezone: str = "Asia/Shanghai") -> str:
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def timestamp() -> float:
    return time.time()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def normalize_text(text: str) -> str:
    """全角转半角、去控制字符、压缩空白。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


_QUALITY_MARK = re.compile(
    r"(?i)\b(4k|8k|uhd|fhd|full\s*hd|hd|sd|1080p|1080i|720p|2160p|"
    r"high\s*definition|超清|高清|标清|蓝光)\b"
)


def strip_quality_mark(text: str) -> str:
    """去掉频道名里的画质标记与常见元数据标记，返回干净名称。"""
    cleaned = _QUALITY_MARK.sub(" ", normalize_text(text))
    # iptv-org 等公开索引会在名称里附加 [Geo-blocked] / [Not 24/7] 等状态标记
    cleaned = re.sub(
        r"\[[^\]]*(?:geo-?blocked|not\s*24/7|offline|down|backup|timeout)[^\]]*\]",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"[\[\(（]\s*[\]\)）]", " ", cleaned)
    cleaned = re.sub(r"[\s\-_|·,，]+$", "", cleaned)
    cleaned = re.sub(r"^[\s\-_|·,，]+", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip() or normalize_text(text)


def claimed_height(text: str) -> int:
    """从名称/属性中推断标称分辨率（仅作辅助线索，不作为最终判据）。"""
    t = text.lower()
    if re.search(r"4k|8k|uhd|2160|超清", t):
        return 2160
    if re.search(r"1080|fhd|full\s*hd|蓝光", t):
        return 1080
    if re.search(r"720", t):
        return 720
    return 0


def is_fetchable(url: str, allow_local: bool) -> bool:
    """只允许 http/https；本地文件仅在显式允许时用于自测。"""
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https"):
        return bool(parsed.netloc)
    if allow_local and (parsed.scheme == "file" or not parsed.scheme):
        return bool(parsed.path or url)
    return False


def read_local(path: str, max_bytes: int = 8 * 1024 * 1024) -> str:
    p = Path(path)
    raw = p.read_bytes()[:max_bytes]
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def http_get(
    url: str,
    *,
    timeout: float = 8.0,
    max_bytes: int = 8 * 1024 * 1024,
    user_agent: str = "LiveHDHub/1.0",
    headers: dict | None = None,
    allow_local: bool = False,
) -> tuple[str, dict]:
    """读取文本资源，返回 (text, meta)。带大小上限与超时，防止恶意/超长响应。

    本地路径（无 scheme）仅在 allow_local=True 时读取，用于离线自测。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file" or (not parsed.scheme and allow_local):
        path = parsed.path if parsed.scheme == "file" else url
        return read_local(path, max_bytes), {"status": 200, "local": True, "url": url}
    if not is_fetchable(url, allow_local):
        raise ValueError(f"不支持的 URL 协议: {url[:80]}")

    req_headers = {"User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        meta = {
            "status": resp.status,
            "content_type": resp.headers.get("Content-Type", ""),
            "ttfb_ms": round((time.perf_counter() - started) * 1000, 1),
            "bytes": len(raw),
            "url": resp.geturl(),
        }
    text = raw.decode("utf-8-sig", "replace")
    if len(text.encode("utf-8", "replace")) > max_bytes:
        text = text[:max_bytes]
    return text, meta


def absolute_url(base: str, ref: str) -> str:
    """解析播放列表里的相对地址。"""
    ref = ref.strip()
    if ref.startswith(("http://", "https://")):
        return ref
    if ref.startswith("//"):
        scheme = urllib.parse.urlparse(base).scheme or "https"
        return f"{scheme}:{ref}"
    return urllib.parse.urljoin(base, ref)


def parse_attrs(line: str) -> dict[str, str]:
    """解析 M3U #EXTINF 上的 key=\"value\" 属性。"""
    attrs: dict[str, str] = {}
    for match in re.finditer(r'([\w-]+)\s*=\s*"([^"]*)"', line):
        attrs[match.group(1).lower()] = match.group(2)
    return attrs
