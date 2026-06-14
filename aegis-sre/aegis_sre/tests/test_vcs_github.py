"""Tests for the GitHubProvider API path (PyGithub mocked, no network)."""

import asyncio
import types

from aegis_sre.orchestrator import vcs_provider as vp
from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent

TELE = TelemetryEvent(event_id="abcd1234ef", service_name="svc", crash_log="boom")


def _patch():
    return CodePatch(file_path="app/main.py", target_content="old", replacement_content="new",
                     root_cause_analysis="rc", explanation="why")


class _Contents:
    def __init__(self, content=b"file-bytes"):
        self.path = "app/main.py"
        self.sha = "sha123"
        self.decoded_content = content


class _PR:
    html_url = "https://github.com/org/repo/pull/7"


class _Repo:
    default_branch = "main"
    def __init__(self): self.created = {}
    def get_git_ref(self, ref): return types.SimpleNamespace(object=types.SimpleNamespace(sha="basesha"))
    def create_git_ref(self, ref, sha): self.created["ref"] = ref
    def get_contents(self, path, ref=None): return _Contents()
    def update_file(self, *a, **k): self.created["updated"] = True
    def create_pull(self, **kw): self.created["pull"] = kw; return _PR()


class _Github:
    def __init__(self, repo): self._repo = repo
    def get_repo(self, name): return self._repo


def test_github_create_pull_request_via_api(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    prov = vp.GitHubProvider("org/repo")
    repo = _Repo()
    prov.g = _Github(repo)  # inject fake PyGithub
    url = asyncio.run(prov.create_pull_request(_patch(), TELE))
    assert url == "https://github.com/org/repo/pull/7"
    assert repo.created["ref"] == "refs/heads/aegis-fix-abcd1234"
    assert repo.created["pull"]["head"] == "aegis-fix-abcd1234"
    assert repo.created["pull"]["base"] == "main"


def test_github_fetch_file_content_via_api(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    prov = vp.GitHubProvider("org/repo")
    prov.g = _Github(_Repo())
    content = asyncio.run(prov.fetch_file_content("app/main.py"))
    assert content == "file-bytes"


def test_github_pr_api_error_returns_mock(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    prov = vp.GitHubProvider("org/repo")
    class Boom:
        def get_repo(self, name): raise RuntimeError("api 500")
    prov.g = Boom()
    url = asyncio.run(prov.create_pull_request(_patch(), TELE))
    assert url == "mock-github-pr-url"
