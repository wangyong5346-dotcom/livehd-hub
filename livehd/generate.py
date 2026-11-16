"""输出层：生成固定订阅文件（M3U / TXT / JSON）、质量报告与状态页。"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .models import ScoredStream
from .util import LOG, now_iso


def fixed_links(output_dir: str, publish_folder: str = "docs") -> dict[str, str]:
    """根据 Actions 注入的环境变量推导固定订阅链接；本地运行时给出占位模板。

    publish_folder 是仓库内真正对外发布的目录，与试运行/自测的临时输出目录解耦，
    避免状态页里出现指向 .tmp/xxx 的无效链接。
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "<OWNER>/<REPO>")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    owner = repo.split("/")[0] if "/" in repo else "<OWNER>"
    name = repo.split("/")[-1] if "/" in repo else "<REPO>"
    folder = publish_folder or Path(output_dir).name
    base_raw = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{folder}"
    base_cdn = f"https://cdn.jsdelivr.net/gh/{owner}/{name}@{branch}/{folder}"
    # Pages 以 docs/ 作为站点根目录发布，URL 里不带 /docs
    owner_host = owner if owner.startswith("<") else owner.lower()
    base_pages = f"https://{owner_host}.github.io/{name}"
    return {
        "m3u_raw": f"{base_raw}/live.m3u",
        "m3u_jsdelivr": f"{base_cdn}/live.m3u",
        "m3u_pages": f"{base_pages}/live.m3u",
        "txt_raw": f"{base_raw}/live.txt",
        "txt_jsdelivr": f"{base_cdn}/live.txt",
        "status_json": f"{base_raw}/status.json",
        "repository": repo,
        "branch": branch,
        "_note": "URL 固定不变，内容由 Actions 定时刷新；播放器只需订阅一次。",
    }


def _m3u(results: dict[str, list[ScoredStream]], cfg) -> str:
    out = cfg["output"]
    lines = ["#EXTM3U"]
    if out.get("tvg_url"):
        lines[0] += f' x-tvg-url="{html.escape(out["tvg_url"], quote=True)}"'
    for name, items in results.items():
        for item in items:
            s = item.stream
            attrs = [f'tvg-id="{html.escape(s.tvg_id or name, quote=True)}"',
                     f'tvg-name="{html.escape(name, quote=True)}"']
            if s.tvg_logo:
                attrs.append(f'tvg-logo="{html.escape(s.tvg_logo, quote=True)}"')
            attrs.append(f'group-title="{html.escape(s.group, quote=True)}"')
            label = name
            if out.get("show_quality_info"):
                label += f" $[{item.height}p|{item.latency_ms:.0f}ms|{item.speed_mbps:.1f}M|{item.score:.0f}分]"
            lines.append(f'#EXTINF:-1 {" ".join(attrs)},{label}')
            lines.append(s.url)
    return "\n".join(lines) + "\n"


def _txt(results: dict[str, list[ScoredStream]], cfg) -> str:
    groups: dict[str, list[str]] = {}
    for name, items in results.items():
        for item in items:
            groups.setdefault(item.stream.group, [])
            line = f"{name},{item.stream.url}"
            if line not in groups[item.stream.group]:
                groups[item.stream.group].append(line)
    lines: list[str] = []
    for group, entries in groups.items():
        lines.append(f"{group},#genre#")
        lines.extend(entries)
    return "\n".join(lines) + "\n"


def _json(results: dict[str, list[ScoredStream]], cfg) -> str:
    payload = {
        "title": cfg["output"]["title"],
        "generated_at": now_iso(cfg["output"]["timezone"]),
        "channels": [
            {
                "name": name,
                "group": items[0].stream.group if items else "其他",
                "streams": [i.as_dict() for i in items],
            }
            for name, items in results.items()
            if items
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _html(results: dict[str, list[ScoredStream]], report: dict, links: dict) -> str:
    esc = html.escape
    total = sum(len(v) for v in results.values())
    dist = report.get("resolution_distribution", {})
    rows = []
    for name, items in results.items():
        for i, item in enumerate(items):
            rows.append(
                "<tr>"
                f"<td>{esc(name) if i == 0 else ''}</td>"
                f"<td>{esc(item.stream.group)}</td>"
                f"<td class='mono'>{item.width}x{item.height}</td>"
                f"<td>{item.score:.1f}</td>"
                f"<td>{item.latency_ms:.0f} ms</td>"
                f"<td>{item.speed_mbps:.2f} Mbps</td>"
                f"<td>{item.success_rate:.0%}</td>"
                f"<td class='mono small'>{esc(item.resolution_source)}</td>"
                f"<td class='mono small url'><a href='{esc(item.stream.url, quote=True)}' target='_blank' rel='noopener'>{esc(item.stream.url[:58])}…</a></td>"
                "</tr>"
            )
    link_items = "".join(
        f"<li><span>{esc(k)}</span><code id='l-{esc(k)}'>{esc(v)}</code>"
        f"<button onclick=\"copyLink('l-{esc(k)}')\">复制</button></li>"
        for k, v in links.items() if not k.startswith("_") and k not in ("repository", "branch")
    )
    dist_text = ", ".join(f"{esc(k)}: {v}" for k, v in dist.items()) or "无"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(report.get('title', 'LiveHD Hub'))} - 运行状态</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:32px; background:#f6f8fb; color:#16202e; font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; }}
h1 {{ font-size:24px; margin:0 0 6px; }} .sub {{ color:#62728a; margin-bottom:24px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin-bottom:24px; }}
.card {{ background:#fff; border:1px solid #e3e9f2; border-radius:12px; padding:16px 18px; }}
.card b {{ display:block; font-size:26px; color:#1565d8; }} .card span {{ color:#62728a; font-size:13px; }}
section {{ background:#fff; border:1px solid #e3e9f2; border-radius:12px; padding:18px 20px; margin-bottom:20px; }}
h2 {{ font-size:17px; margin:0 0 12px; }}
ul {{ list-style:none; padding:0; margin:0; }}
li {{ display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px dashed #e8edf4; flex-wrap:wrap; }}
li span {{ width:150px; color:#62728a; font-size:13px; }}
code {{ background:#f2f5fa; padding:5px 8px; border-radius:6px; font-size:12px; overflow-wrap:anywhere; flex:1; }}
button {{ border:1px solid #c9d7ea; background:#fff; color:#1565d8; border-radius:6px; padding:5px 12px; cursor:pointer; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:9px 8px; border-bottom:1px solid #edf1f7; }}
th {{ color:#62728a; font-weight:600; background:#fafbfd; }}
.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.small {{ font-size:12px; }} .url {{ max-width:230px; }}
a {{ color:#1565d8; text-decoration:none; }}
.note {{ color:#62728a; font-size:13px; }}
</style></head><body><div class="wrap">
<h1>{esc(report.get('title', 'LiveHD Hub'))}</h1>
<div class="sub">最近更新：{esc(report.get('generated_at',''))} · 自动运行 · 仅聚合用户自行配置的合法播放列表</div>
<div class="cards">
<div class="card"><b>{len(results)}</b><span>频道</span></div>
<div class="card"><b>{total}</b><span>可用高清源</span></div>
<div class="card"><b>{report.get('candidates',0)}</b><span>候选流</span></div>
<div class="card"><b>{report.get('quick_passed',0)}</b><span>快速探测通过</span></div>
<div class="card"><b>{report.get('deep_checked',0)}</b><span>ffprobe 复核</span></div>
<div class="card"><b>{report.get('duration_seconds',0):.0f}s</b><span>本轮耗时</span></div>
</div>
<section><h2>固定订阅链接（内容定时刷新，地址不变）</h2><ul>{link_items}</ul>
<p class="note">分辨率分布：{dist_text} · 硬门槛：≥ {report.get('min_height',1080)}p，最长延迟 {report.get('max_latency_ms',1500)} ms</p></section>
<section><h2>频道明细</h2><div style="overflow-x:auto"><table>
<thead><tr><th>频道</th><th>分组</th><th>分辨率</th><th>评分</th><th>延迟</th><th>速率</th><th>成功率</th><th>分辨率依据</th><th>地址</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="9">本轮无符合门槛的源，请检查源清单与阈值配置。</td></tr>'}</tbody>
</table></div></section>
<p class="note">合规说明：本项目不提供、不托管任何节目内容；请仅添加有权访问的公开或已授权播放列表，并遵守所在地法律法规与平台条款。</p>
</div><script>
function copyLink(id) {{ const t=document.getElementById(id).textContent; navigator.clipboard.writeText(t).then(()=>{{}}); }}
</script></body></html>
"""


def write_outputs(results: dict[str, list[ScoredStream]], report: dict, cfg, output_dir: str | Path) -> list[str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    links = fixed_links(str(out), cfg.data.get("publish_folder", "docs"))
    written: list[str] = []
    o = cfg["output"]

    if o.get("m3u", True):
        p = out / "live.m3u"
        p.write_text(_m3u(results, cfg), encoding="utf-8")
        written.append(str(p))
    if o.get("txt", True):
        p = out / "live.txt"
        p.write_text(_txt(results, cfg), encoding="utf-8")
        written.append(str(p))
    if o.get("json", True):
        p = out / "channels.json"
        p.write_text(_json(results, cfg), encoding="utf-8")
        written.append(str(p))

    status = dict(report)
    status["subscription_links"] = links
    status["files"] = [Path(x).name for x in written]
    status["title"] = o.get("title", "LiveHD Hub")
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append(str(out / "status.json"))

    if o.get("html", True):
        p = out / "index.html"
        p.write_text(_html(results, status, links), encoding="utf-8")
        written.append(str(p))

    LOG.info("输出完成：%s", ", ".join(Path(x).name for x in written))
    return written
