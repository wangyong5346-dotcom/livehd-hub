"""发布脚本的离线测试：验证 REST API 直传兜底，不联网、不需要 token。"""

from __future__ import annotations

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import publish_github as pub  # noqa: E402


class FakeGitHub:
    def __init__(self, existing: bool = False):
        self.existing = existing
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, body: dict | None = None, quiet_404: bool = False):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return (200, {"object": {"sha": "base-sha"}}) if self.existing else (404, {})
        if self.existing and method == "GET" and path.endswith("/git/commits/base-sha"):
            return 200, {"tree": {"sha": "base-tree"}}
        if method == "POST" and path.endswith("/git/blobs"):
            return 201, {"sha": f"blob-{len(self.calls)}"}
        if method == "POST" and path.endswith("/git/trees"):
            return 201, {"sha": "tree-sha"}
        if method == "POST" and path.endswith("/git/commits"):
            return 201, {"sha": "commit-sha"}
        return 200, {}


class PublisherTests(unittest.TestCase):
    def _tracked(self):
        return [p for p in pub.git(["ls-files", "-z"]).stdout.split("\x00") if p]

    def test_api_commit_to_empty_repo(self):
        gh = FakeGitHub(existing=False)
        count = pub.api_commit_all(gh, "alice", "livehd-hub")
        tracked = self._tracked()
        self.assertEqual(count, len(tracked))
        self.assertGreater(count, 20)
        tree = next(b for m, p, b in gh.calls if m == "POST" and p.endswith("/git/trees"))
        self.assertEqual(len(tree["tree"]), len(tracked))
        self.assertNotIn("base_tree", tree)
        commit = next(b for m, p, b in gh.calls if m == "POST" and p.endswith("/git/commits"))
        self.assertNotIn("parents", commit)
        self.assertTrue(any(m == "POST" and p.endswith("/git/refs") for m, p, _ in gh.calls))

    def test_api_commit_to_existing_repo(self):
        gh = FakeGitHub(existing=True)
        pub.api_commit_all(gh, "alice", "livehd-hub")
        tree = next(b for m, p, b in gh.calls if m == "POST" and p.endswith("/git/trees"))
        self.assertEqual(tree["base_tree"], "base-tree")
        commit = next(b for m, p, b in gh.calls if m == "POST" and p.endswith("/git/commits"))
        self.assertEqual(commit["parents"], ["base-sha"])
        self.assertTrue(any(m == "PATCH" and p.endswith("/git/refs/heads/main") for m, p, _ in gh.calls))

    def test_preflight_clean(self):
        self.assertEqual(pub.preflight(), [], "交付仓库应保持干净、位于 main 分支")


class EmptyRepoTests(unittest.TestCase):
    """新建且未初始化的仓库会返回 409 Git Repository is empty，需按分支不存在处理。"""

    @staticmethod
    def _http_error(message: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://api.github.com/repos/alice/demo/git/ref/heads/main",
            409, "Conflict", {}, io.BytesIO(message.encode()),
        )

    def test_409_empty_repo_is_treated_as_missing_ref(self):
        gh = pub.GitHub("dummy")
        err = self._http_error('{"message":"Git Repository is empty."}')
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(gh.request("GET", "/git/ref/heads/main", quiet_404=True), (404, {}))

    def test_other_409_still_raises(self):
        gh = pub.GitHub("dummy")
        err = self._http_error('{"message":"Merge conflict"}')
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError):
                gh.request("GET", "/git/ref/heads/main", quiet_404=True)


class PagesTests(unittest.TestCase):
    class FakeGH:
        def __init__(self, existing: bool):
            self.existing = existing
            self.calls: list[tuple[str, str, dict | None]] = []

        def request(self, method, path, body=None, quiet_404=False):
            self.calls.append((method, path, body))
            if method == "GET":
                return (200, {}) if self.existing else (404, {})
            return 201, {}

    def test_enable_pages_from_docs(self):
        gh = self.FakeGH(existing=False)
        self.assertTrue(pub.ensure_pages(gh, "alice", "demo", "main"))
        self.assertTrue(any(
            m == "POST" and p.endswith("/pages") and b["source"] == {"branch": "main", "path": "/docs"}
            for m, p, b in gh.calls
        ))

    def test_reuse_existing_pages(self):
        gh = self.FakeGH(existing=True)
        self.assertTrue(pub.ensure_pages(gh, "alice", "demo", "main"))
        self.assertFalse(any(m == "POST" for m, _, _ in gh.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
