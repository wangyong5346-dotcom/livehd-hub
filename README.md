# LiveHD Hub · 高清直播源聚合与固定订阅

自动从**你自己配置**的公开/授权播放列表中采集流地址，经过「响应速度 + 下载速率 + 多轮稳定性 + 真实分辨率」质检，只保留 **1080p 及以上**的可用源，生成固定不变的 M3U / TXT 订阅链接，并由 GitHub Actions 定时刷新。

> ⚠️ **合规声明（务必先读）**
> 本项目是一个**播放列表聚合与质量检测框架**，不收集、不存储、不托管、不分发任何电视节目内容，仓库内**不包含任何直播流地址**（`config/sources.json` 默认全部关闭）。
> 使用者必须只添加自己**有权访问**的公开或已授权播放列表，并遵守所在国家/地区的法律法规、版权要求以及 GitHub 服务条款；**不得用于传播未授权内容、绕过版权/地域保护措施或商业用途**。收到权利人通知时应立即从配置中移除相应源。使用者对自己配置的源及使用后果独立负责。

---

## 一、GitHub 同类开源项目调研（2026-11-16 实测）

| 项目 | Star | 定位 | 可借鉴 | 局限 |
| --- | --- | --- | --- | --- |
| [iptv-org/iptv](https://github.com/iptv-org/iptv) | 144.1k | 全球公开频道元数据 + M3U 播放列表库（Unlicense） | 每日 Actions 更新、`streams/` 结构化数据、频道元数据规范 | 只做链接校验，不做测速/分辨率质检，不区分画质 |
| [Guovin/iptv-api](https://github.com/Guovin/iptv-api)（原 Guovin/TV） | 26.2k | 采集 + 测速 + 生成 M3U/TXT/API，Docker/GUI/Actions | 订阅源聚合、频道模板、别名/黑白名单、定时更新、结果缓存 | 默认门槛仅 `1280x720`；分辨率获取需另装 FFmpeg；依赖 Pipenv 全家桶；**明确禁止在 fork 仓库加 `schedule` 或把结果提交回 Git**（改为 Pages Artifact / Release） |
| [vbskycn/iptv](https://github.com/vbskycn/iptv) | 10.4k | 双栈可用源结果仓库（GPL-3.0） | IPv4/IPv6 双栈输出的思路 | 是「结果仓库」，不是可定制的质检框架 |
| [joevess/IPTV](https://github.com/joevess/IPTV) | 10.3k | 聚合多站点源并择优 | 「择取分辨率、速度最佳视频流」的排序思想 | 2024 年底后停更 |
| [KimWang1978/collect-tv-txt](https://github.com/KimWang1978/collect-tv-txt) | 2.0k | 每日自动整理 TXT 源 | 极简定时更新范式 | 无质检、无评分 |
| [zhimin-dev/iptv-checker](https://github.com/zhimin-dev/iptv-checker) / [HerbertHe/iptv-sources](https://github.com/HerbertHe/iptv-sources) | — | 可用性检测 / 多源 + EPG 聚合 | 并发检测、EPG 聚合 | 侧重点不同，非端到端订阅生成 |

**本项目以 Guovin/iptv-api 的「采集 → 校验 → 生成 → 定时更新」流程为基线，借鉴 iptv-org 的结构化数据与 Actions 更新机制，做了以下优化：**

1. **1080p 硬门槛 + 真实分辨率复核**：用 `ffprobe` 拉流读取真实 `width/height`，不靠 URL 或频道名里的「1080/高清」字样；上游默认 720p 且需手动装 FFmpeg。
2. **HLS master 清单解析**：读取 `#EXT-X-STREAM-INF` 的 `RESOLUTION=` 并实测分片下载速率，比盲目 ffprobe 更快更省流量。
3. **稳定性记忆（EWMA）**：多轮采样计算成功率与抖动，跨运行保存历史质量；连续失败源进入隔离区、到期自动复活，避免每轮浪费探测预算。
4. **两阶段漏斗 + 全局时间预算**：Stage A 高并发快速探测（24 线程）→ Stage B 低并发 ffprobe 精测（每频道 Top-K），超时自动取消剩余任务，保证 Actions 不超时。
5. **零第三方依赖**：纯 Python 标准库（3.10+），Actions 冷启动快、无 `pip install` 失败风险；`ffprobe` 为可选增强。
6. **真冗余**：每频道保留 Top-N，且限制同一主机最多 1 条，避免「多个地址其实挂在同一台服务器」的假备份。
7. **多通道固定订阅链接**：raw、jsDelivr CDN（国内较快）、GitHub Pages 三种固定地址 + 可视化状态页 + 质量报告 JSON。
8. **平台友好**：提供 `commit`（写回自有仓库）与 `pages`（只传 Artifact、不写回 Git）两种发布模式，遵守上游关于「fork 不加 cron」的约定。

---

## 二、架构

```mermaid
flowchart LR
  A[用户配置的源清单<br/>sources.json] --> B[采集层<br/>M3U / TXT / 订阅递归 / GitHub 目录]
  B --> C[归一化<br/>别名·分组·去重·黑名单]
  C --> D[Stage A 快速探测<br/>延迟·速率·HLS 分辨率·多轮采样]
  D --> E[Stage B ffprobe<br/>真实分辨率复核]
  E --> F[五维评分 + 硬门槛<br/>每频道 Top-N]
  F --> G[M3U / TXT / JSON / 状态页]
  G --> H[Actions 定时提交 或 Pages 发布]
  H --> I[固定订阅链接<br/>raw / jsDelivr / Pages]
```

评分 = `30% 分辨率 + 25% 响应延迟 + 20% 下载速率 + 15% 稳定性(成功率×抖动) + 10% 历史表现`（权重可在 `selection.weights` 调整）。

硬门槛：`真实高度 ≥ 1080`、`宽度 ≥ 1280`、`延迟 ≤ 1500ms`、`速率 ≥ 1.0Mbps`、`多轮成功率 ≥ 0.6`。

---

## 三、目录结构

```
livehd-hub/
├── main.py                     # CLI 入口（含离线自测）
├── config/
│   ├── settings.json           # 阈值、并发、输出、评分权重
│   ├── sources.json            # ★ 采集源清单（默认全部关闭，需自行填写）
│   └── channels.json           # 频道别名 / 分组 / 白名单
├── livehd/
│   ├── collect.py              # 采集与 M3U/TXT 解析、GitHub 目录发现
│   ├── normalize.py            # 频道名归一化
│   ├── probe.py                # 快速探测、HLS 解析、ffprobe
│   ├── score.py                # 硬门槛与加权评分
│   ├── state.py                # 历史质量缓存与隔离
│   ├── pipeline.py             # 两阶段编排 + 时间预算
│   └── generate.py             # 订阅文件、报告、状态页
├── docs/                       # ★ 生成产物（订阅链接指向这里）
├── tests/                      # 离线单元测试 + 样例
└── .github/workflows/update.yml# ★ 定时自动运行
```

---

## 四、本地使用

```bash
cd livehd-hub
python3 --version                 # 需要 3.10+（开发验证环境 3.13）
python main.py --self-test        # 离线自测：不联网、不需要 ffprobe，验证全链路
python -m unittest discover -s tests -v   # 单元测试

# 真实运行（先在 config/sources.json 启用你有权访问的源）
python main.py --workers 16 --rounds 2 --min-height 1080
python main.py --local-sources ./my.m3u ./my.txt --no-ffprobe --allow-unverified
python main.py --dry-run          # 结果写到 .tmp/dryrun，不污染 docs
```

常用参数：`--min-height`、`--rounds`、`--workers`、`--top-n`、`--max-candidates`、`--time-budget`、`--tvg-url`、`--no-ffprobe`、`--allow-unverified`。

> 本机（macOS）没有 `ffprobe` 也能跑：`--no-ffprobe --allow-unverified` 会降级为「按清单标称分辨率」过滤；GitHub Actions 的 Ubuntu runner 自带 ffmpeg，走完整严格模式。

---

## 五、配置数据源（关键一步）

编辑 `config/sources.json`，把 `enabled` 改为 `true` 并填写你有合法访问权的地址：

| `type` | 说明 | 关键字段 |
| --- | --- | --- |
| `playlist` | 直接解析 M3U / TXT 播放列表 | `url`, `format`(auto/m3u/txt), 可选 `headers` |
| `subscription` | 每行是一个新的播放列表地址，自动一层展开（最多 40 个） | `url` |
| `github_dir` | 通过 GitHub API 发现公开仓库目录里的 `.m3u/.m3u8/.txt` | `repo`(owner/repo), `path`, `max_files` |

```json
{
  "sources": [
    { "name": "我的授权源", "type": "playlist", "url": "https://your-domain/playlist.m3u", "format": "auto", "enabled": true }
  ],
  "blacklist": { "url_keywords": [], "domains": [], "channel_keywords": [] }
}
```

`config/channels.json` 可设置 `only_whitelist: true` 只输出指定频道，并用 `aliases` 把「CCTV1 / CCTV-1 综合 / 中央一台」合并为同一频道。`config/settings.json` 可调整全部阈值；其中 `publish_folder` 固定为 `docs`，因此即使用 `--dry-run` / `--self-test` 把结果写到临时目录，状态页里给出的订阅链接也始终指向真实发布目录。若你用 `--output` 改了正式输出目录，请同步修改 `publish_folder`。

> 提示：公开合法频道列表（如 iptv-org 的 Unlicense 播放列表）可作为合规数据源示例；请在启用前自行确认其使用条款与你所在地区的合规要求。

---

## 六、配置 GitHub 自动运行

### 方式 A：一键脚本（推荐）

`scripts/publish_github.py` 会用一条命令完成：创建仓库 → 推送 `main` → 开启 Actions 写权限 → 启用 `update.yml` → **自动启用 Pages（默认分支 `/docs`）** → 触发首次运行 → 打印固定订阅链接。凭据只存在于进程内存，不写入文件、不打印，也不会出现在 git remote 里。脚本会自动处理「新建空仓库 Git 数据库未激活」（`409 Git Repository is empty`），并跟随仓库实际默认分支。

```bash
python3 scripts/publish_github.py --check                    # 先预检本地状态与凭据
GITHUB_TOKEN=<PAT> python3 scripts/publish_github.py --repo <OWNER>/livehd-hub
# 或者已登录 gh CLI 时：python3 scripts/publish_github.py --repo <OWNER>/livehd-hub
```

凭据三选一：`GITHUB_TOKEN` 环境变量、凭据文件 `~/.workbuddy/livehd_github_token`（脚本只读取、不回显、不落库），或本机 `gh auth login` 登录态。PAT 需要 `repo` + `workflow` 权限。默认创建 **public** 仓库 —— 固定订阅链接要能被播放器匿名访问，jsDelivr 也只索引公开仓库。脚本幂等：仓库已存在则复用，重复运行安全。

若网络访问不到 `github.com` 的 git 传输（部分代理环境会这样），加 `--api-only`：脚本改走 `api.github.com` 的 Git Data API，把全部文件放进一次提交；不加该参数时，git push 失败也会自动回退到这条路径。REST API 模式不需要 SSH 或 git 传输。

> 若是在 WorkBuddy 里启用了 GitHub 连接器，也可以直接让助手代推，无需手工执行脚本。

### 方式 B：手动步骤

1. **新建自己的独立仓库**（不要 fork 其它聚合仓库），把本项目推上去，默认分支为 `main`：
   ```bash
   cd livehd-hub
   git init && git add . && git commit -m "init: LiveHD Hub"
   git remote add origin https://github.com/<OWNER>/<REPO>.git
   git push -u origin main
   ```
2. **Settings → Actions → General → Workflow permissions** 勾选 **Read and write permissions**（提交 `docs/` 需要写权限）。
3. **启用数据源**：编辑 `config/sources.json`（不启用任何源时，生成的是空列表 + 状态报告，属正常行为）。
4. **Actions → 更新高清直播订阅 → Run workflow** 手动跑一次；之后按 `cron: 17 */6 * * *`（UTC 00/06/12/18:17，即北京时间约 2/8/14/20 点）自动运行，配置变更推送也会立即触发。
5. **Pages（一键脚本已自动配置）**：默认设为 *Deploy from a branch → `main` / `docs`*，状态页与订阅文件同址发布，生成物提交回仓库后 Pages 自动重建。若不想把生成物写回 Git（例如 fork 场景），改用 Artifact 模式：在 **Settings → Secrets and variables → Actions → Variables** 新建仓库变量 `PUBLISH_MODE = pages`（或 `both`），并把 **Settings → Pages** 的 Source 改为 **GitHub Actions**；此时请只使用 Pages 链接，raw 链接不再更新。
6. 在每次运行的 **Step Summary** 里直接复制固定订阅链接。

工作流内置：离线自测 → 历史状态缓存（`actions/cache`）→ 采集质检 → 提交/Pages 发布 → 输出订阅链接。`timeout-minutes: 25`，流水线自身还有 `--time-budget 780` 秒的软预算。

---

## 七、固定订阅链接（地址不变，内容定时刷新）

**已部署实例（2026-11-16 上线，仓库 `wangyong5346-dotcom/livehd-hub`）**：

| 用途 | 固定链接 |
| --- | --- |
| M3U（raw） | `https://raw.githubusercontent.com/wangyong5346-dotcom/livehd-hub/main/docs/live.m3u` |
| M3U（jsDelivr CDN） | `https://cdn.jsdelivr.net/gh/wangyong5346-dotcom/livehd-hub@main/docs/live.m3u` |
| TXT | `https://raw.githubusercontent.com/wangyong5346-dotcom/livehd-hub/main/docs/live.txt` |
| M3U（Pages） | `https://wangyong5346-dotcom.github.io/livehd-hub/live.m3u` |
| 状态页 / 质量报告 | `https://wangyong5346-dotcom.github.io/livehd-hub/` · `docs/status.json` |
| Actions 运行记录 | `https://github.com/wangyong5346-dotcom/livehd-hub/actions/workflows/update.yml` |

> 上线时 `config/sources.json` 全部关闭，因此订阅文件是空的（仅 `#EXTM3U`），这是合规设计的**预期状态**。启用自有或已授权数据源后，下一次定时运行（或手动 `Run workflow`）即会填充通过 1080p 复核的源。

通用模板 —— 把 `<OWNER>/<REPO>` 换成你的仓库：

| 用途 | 链接 |
| --- | --- |
| M3U（raw，兼容性最好） | `https://raw.githubusercontent.com/<OWNER>/<REPO>/main/docs/live.m3u` |
| M3U（jsDelivr CDN，国内较快） | `https://cdn.jsdelivr.net/gh/<OWNER>/<REPO>@main/docs/live.m3u` |
| TXT（TVBox 等） | `https://raw.githubusercontent.com/<OWNER>/<REPO>/main/docs/live.txt` |
| M3U（Pages） | `https://<OWNER>.github.io/<REPO>/live.m3u` |
| 状态页 / 质量报告 | `https://<OWNER>.github.io/<REPO>/` · `docs/status.json` · `docs/channels.json` |

播放器（VLC、PotPlayer、TiviMate、TVBox 等）填入上述任一 M3U/TXT 地址即可，一次订阅长期有效。若用 Pages 模式，请只使用 Pages 地址（raw 地址不会随 Artifact 更新）。

---

## 八、排错

| 现象 | 检查 |
| --- | --- |
| 订阅是空的 | `config/sources.json` 是否启用源；看 `docs/status.json` 的 `collect.errors` 与 `reject_reasons` |
| Actions 在「提交订阅文件」步骤失败 | 多为两次运行并发导致 push 非快进；工作流已改为每轮基于 `origin/main` 重建提交并重试 3 次。若仍失败，检查 Workflow permissions 是否为 Read and write |
| 大量「ffprobe 未确认真实分辨率」 | runner 是否装好 ffprobe；或流需要 Referer（在 M3U 里用 `#KODIPROP`/`#EXTVLCOPT` 声明）；必要时 `--allow-unverified` |
| 大量「下载速率不足」 | 把 `quality.min_speed_mbps` 调低（如 `0.2`），或源本身只允许特定地区访问 |
| 运行太慢 | 降低 `quick_workers` / `max_candidates_per_channel` / `ffprobe_top_k`，缩短 `time_budget_seconds` |
| 频道逐渐变少 | 属正常衰减；`state.py` 会在隔离期后自动复活源，也可多配置几个合规数据源 |

---

## 九、许可

代码采用 **MIT License**（见 `LICENSE`）。本许可是对工具代码本身的授权，**不授予任何节目内容的传播权**。合规声明见本文开头，使用即视为已阅读并同意。
