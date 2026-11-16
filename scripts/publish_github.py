#!/usr/bin/env python3
"""一键把 LiveHD Hub 发布到 GitHub 并启用自动运行。

凭据来源（按优先级；token 不打印、不写入仓库、不出现在 git remote）：
  1. 环境变量 GITHUB_TOKEN / GH_TOKEN（PAT，需 repo + workflow 权限）
  2. 凭据文件 ~/.workbuddy/livehd_github_token（或 GITHUB_TOKEN_FILE 指定的路径）
  3. 已登录的 gh CLI（自动执行 `gh auth token` 取 token）

用法：
  python3 scripts/publish_github.py --check
  python3 scripts/publish_github.py --repo <owner>/<repo>
  python3 scripts/publish_github.py --repo <owner>/<repo> --visibility public --no-run
  python3 scripts/publish_github.py --repo <owner>/<repo> --api-only   # 只走 REST API 直传

做的事：创建仓库（若不存在）→ 设置 origin 并推送 main → 打开 Actions 写权限
→ 启用 update.yml 工作流 → 触发首次运行 → 打印固定订阅链接。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
DESCRIPTION = "高清直播源聚合质检与固定订阅（工具框架，不含任何直播源）"
WORKFLOW = ".github/workflows/update.yml"


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None, quiet_404: bool = False):
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "livehd-hub-publisher",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # 新建且未初始化的仓库，Git Data API 会返回 409 "Git Repository is empty"；
            # 对探测类请求把它等同于 404（分支尚不存在）。
            if quiet_404 and (exc.code == 404 or (exc.code == 409 and "Git Repository is empty" in detail)):
                return 404, {}
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def get_token() -> tuple[str, str]:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"环境变量 {name}"
    token_file = os.environ.get("GITHUB_TOKEN_FILE") or os.path.expanduser("~/.workbuddy/livehd_github_token")
    if os.path.isfile(token_file):
        value = Path(token_file).read_text(encoding="utf-8").strip()
        if value:
            return value, f"凭据文件 {token_file}"
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip(), "gh CLI 登录态"
    return "", ""


def git(args: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, env=env, check=check)


def preflight() -> list[str]:
    problems: list[str] = []
    if not (ROOT / ".git").exists():
        problems.append("本地尚不是 git 仓库")
    for rel in ("README.md", "config/sources.json", "main.py", WORKFLOW):
        if not (ROOT / rel).exists():
            problems.append(f"缺少文件 {rel}")
    if (ROOT / ".git").exists():
        branch = git(["branch", "--show-current"], check=False).stdout.strip()
        if branch != "main":
            problems.append(f"当前分支是 {branch or '(空)'}，脚本按 main 推送")
        status = git(["status", "--porcelain"], check=False).stdout.strip()
        if status:
            problems.append(f"工作区有未提交改动（{len(status.splitlines())} 个文件），请先提交")
    return problems


def push_with_token(owner: str, repo: str, token: str, branch: str = "main") -> None:
    """用临时 GIT_ASKPASS 传入凭据，避免 token 落盘或出现在 git remote 里。"""
    remote_url = f"https://github.com/{owner}/{repo}.git"
    existing = git(["remote", "get-url", "origin"], check=False)
    if existing.returncode == 0:
        git(["remote", "set-url", "origin", remote_url])
    else:
        git(["remote", "add", "origin", remote_url])

    fd, askpass = tempfile.mkstemp(prefix="livehd-askpass-", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write('#!/bin/sh\ncase "$1" in *Username*) echo "x-access-token" ;; *) printf %s "$GH_PUSH_TOKEN" ;; esac\n')
        os.chmod(askpass, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        env = dict(os.environ)
        env.update({"GIT_ASKPASS": askpass, "GH_PUSH_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"})
        proc = git(["push", "-u", "origin", f"HEAD:refs/heads/{branch}"], env=env, check=False)
        if proc.returncode != 0:
            raise RuntimeError("git push 失败: " + (proc.stderr or proc.stdout).replace(token, "***").strip()[:500])
        print("  推送完成：" + (proc.stderr or proc.stdout).replace(token, "***").strip()[-300:])
    finally:
        try:
            os.unlink(askpass)
        except OSError:
            pass


def api_commit_all(gh: GitHub, owner: str, repo: str, branch: str = "main") -> int:
    """git 传输被阻断时的兜底：用 Git Data API 把所有文件放进一次提交。

    只访问 api.github.com，不需要 github.com 的 git 端口，也不需要 SSH。
    新建且未初始化的仓库 Git 数据库是空的，blob 端点会返回
    "409 Git Repository is empty"；此时先用 Contents API 写入 README.md
    生成初始提交以激活 Git 数据库，再继续上传。
    """
    prefix = f"/repos/{owner}/{repo}"
    _, meta = gh.request("GET", prefix)
    branch = meta.get("default_branch") or branch

    status, ref = gh.request("GET", f"{prefix}/git/ref/heads/{branch}", quiet_404=True)
    if status != 200:
        gh.request("PUT", f"{prefix}/contents/README.md", {
            "message": "init: 初始化仓库",
            "content": base64.b64encode((ROOT / "README.md").read_bytes()).decode("ascii"),
        })
        status, ref = gh.request("GET", f"{prefix}/git/ref/heads/{branch}", quiet_404=True)

    paths = [p for p in git(["ls-files", "-z"]).stdout.split("\x00") if p]
    entries: list[dict] = []
    for rel in paths:
        raw = (ROOT / rel).read_bytes()
        _, blob = gh.request("POST", f"{prefix}/git/blobs", {
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        })
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    base_sha = ref.get("object", {}).get("sha") if status == 200 else ""
    tree_body: dict = {"tree": entries}
    if base_sha:
        _, base_commit = gh.request("GET", f"{prefix}/git/commits/{base_sha}")
        tree_body["base_tree"] = base_commit["tree"]["sha"]
    _, tree = gh.request("POST", f"{prefix}/git/trees", tree_body)

    commit_body: dict = {"message": "init: LiveHD Hub 高清直播源聚合质检与自动订阅", "tree": tree["sha"]}
    if base_sha:
        commit_body["parents"] = [base_sha]
    _, commit = gh.request("POST", f"{prefix}/git/commits", commit_body)
    if base_sha:
        gh.request("PATCH", f"{prefix}/git/refs/heads/{branch}", {"sha": commit["sha"]})
    else:
        gh.request("POST", f"{prefix}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    return len(entries)


def ensure_pages(gh: GitHub, owner: str, repo: str, branch: str) -> bool:
    """让状态页链接可用：从默认分支的 /docs 目录部署 Pages（legacy 模式）。"""
    prefix = f"/repos/{owner}/{repo}"
    status, _ = gh.request("GET", f"{prefix}/pages", quiet_404=True)
    if status == 200:
        return True
    try:
        gh.request("POST", f"{prefix}/pages", {
            "build_type": "legacy",
            "source": {"branch": branch, "path": "/docs"},
        })
        return True
    except RuntimeError as exc:
        print("  Pages 自动启用失败（可在 Settings → Pages 手动设为", branch, "/docs）:", str(exc)[:160])
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="发布 LiveHD Hub 到 GitHub 并启用自动运行")
    parser.add_argument("--repo", help="目标仓库 owner/repo；省略则使用 <登录用户名>/livehd-hub")
    parser.add_argument("--visibility", choices=("public", "private"), default="public",
                        help="默认 public：固定订阅链接需要匿名可访问，jsDelivr 也只索引公开仓库")
    parser.add_argument("--check", action="store_true", help="只做本地与凭据预检，不做任何远端操作")
    parser.add_argument("--no-run", action="store_true", help="不触发首次工作流运行")
    parser.add_argument("--api-only", action="store_true", help="跳过 git push，直接用 REST API 创建提交（受限网络推荐）")
    args = parser.parse_args()

    print("== 本地预检 ==")
    problems = preflight()
    for p in problems:
        print("  [问题]", p)
    token, source = get_token()
    print(f"  GitHub 凭据: {'已获取（' + source + '）' if token else '未检测到'}")
    if args.check:
        ok = not problems and bool(token)
        print("  预检结果:", "通过，可执行发布" if ok else "未通过")
        return 0 if ok else 2
    if problems:
        print("请先修复上述问题再发布。")
        return 2
    if not token:
        print("\n未检测到 GitHub 凭据。请任选其一：")
        print("  1) export GITHUB_TOKEN=<你的 PAT，需 repo + workflow 权限>")
        print("  2) 安装并登录 gh CLI：brew install gh && gh auth login")
        print("  3) 在 WorkBuddy 聊天窗口左下角加号菜单启用 GitHub 连接器后，让助手直接代推")
        return 2

    gh = GitHub(token)
    status, user = gh.request("GET", "/user")
    login = user.get("login", "")
    if not login:
        print("无法获取 GitHub 登录用户名，请检查 token 权限。")
        return 2
    print(f"  已认证用户: {login}")

    if args.repo:
        if "/" not in args.repo:
            print("--repo 需要 owner/repo 格式")
            return 2
        owner, repo = args.repo.split("/", 1)
    else:
        owner, repo = login, "livehd-hub"

    print(f"== 目标仓库: {owner}/{repo} ({args.visibility}) ==")
    status, _ = gh.request("GET", f"/repos/{owner}/{repo}", quiet_404=True)
    if status == 404:
        create_path = "/user/repos" if owner.lower() == login.lower() else f"/orgs/{owner}/repos"
        gh.request("POST", create_path, {
            "name": repo,
            "description": DESCRIPTION,
            "private": args.visibility == "private",
            "has_issues": False,
            "has_wiki": False,
            "has_projects": False,
            "auto_init": True,
        })
        print("  已创建仓库（自动初始化）")
    else:
        print("  仓库已存在，复用")

    _, meta = gh.request("GET", f"/repos/{owner}/{repo}")
    branch = meta.get("default_branch") or "main"

    print(f"== 推送代码（目标分支 {branch}）==")
    if args.api_only:
        count = api_commit_all(gh, owner, repo, branch)
        print(f"  REST API 直传完成：{count} 个文件，提交到 {branch}")
    else:
        try:
            push_with_token(owner, repo, token, branch)
        except RuntimeError as exc:
            print("  git 传输不可用，自动改用 REST API 直传:", str(exc)[:110])
            count = api_commit_all(gh, owner, repo, branch)
            print(f"  REST API 直传完成：{count} 个文件，提交到 {branch}")

    # API 直传路径没有经过 push_with_token，仍需把 origin 指向干净的无凭据 URL
    remote_url = f"https://github.com/{owner}/{repo}.git"
    if git(["remote", "get-url", "origin"], check=False).returncode == 0:
        git(["remote", "set-url", "origin", remote_url])
    else:
        git(["remote", "add", "origin", remote_url])

    print("== 配置 Actions ==")
    gh.request("PUT", f"/repos/{owner}/{repo}/actions/permissions",
               {"enabled": True, "allowed_actions": "all"})
    gh.request("PUT", f"/repos/{owner}/{repo}/actions/permissions/workflow",
               {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True})
    print("  已开启 Actions，并授予 GITHUB_TOKEN 写权限（提交 docs/ 必需）")

    status, workflows = gh.request("GET", f"/repos/{owner}/{repo}/actions/workflows")
    target = next((w for w in workflows.get("workflows", []) if w.get("path") == WORKFLOW), None)
    if target:
        gh.request("PUT", f"/repos/{owner}/{repo}/actions/workflows/{target['id']}/enable")
        print(f"  工作流已启用: {target.get('name')}")
    else:
        print("  工作流尚未被 GitHub 索引，稍后会自动出现")

    print("== 配置 Pages 状态页 ==")
    pages_ok = ensure_pages(gh, owner, repo, branch)
    print("  " + ("已启用/复用 Pages：默认分支 /docs，状态页与订阅文件同址发布"
                  if pages_ok else "未启用，状态页链接暂不可用"))

    if not args.no_run:
        try:
            gh.request("POST", f"/repos/{owner}/{repo}/actions/workflows/update.yml/dispatches", {"ref": branch})
            print("  已触发首次运行")
        except RuntimeError as exc:
            print("  首次运行触发失败（可稍后在 Actions 页面手动运行）:", exc)

    print("\n== 固定订阅链接（地址不变，内容每 6 小时刷新）==")
    print(f"  M3U raw      : https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/live.m3u")
    print(f"  M3U jsDelivr : https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/docs/live.m3u")
    print(f"  TXT raw      : https://raw.githubusercontent.com/{owner}/{repo}/{branch}/docs/live.txt")
    print(f"  Pages 状态页 : https://{owner.lower()}.github.io/{repo}/")
    print("\n提醒：公开仓库请勿填入未授权的播放源；源清单在 config/sources.json，默认全部关闭。")
    print("提示：REST API 直传生成的是单次提交，与本地提交历史不同；后续继续本地开发请先 git fetch origin && git rebase origin/" + branch)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print("错误:", exc, file=sys.stderr)
        raise SystemExit(1)
