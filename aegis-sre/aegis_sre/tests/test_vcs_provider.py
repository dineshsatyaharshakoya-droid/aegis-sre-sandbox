"""Tests for VCS provider selection and the non-GitOps provider paths."""

import asyncio
import os

import pytest

from aegis_sre.orchestrator import vcs_provider as vp
from aegis_sre.orchestrator.schemas import CodePatch, TelemetryEvent

TELE = TelemetryEvent(event_id="e1", service_name="svc", crash_log="boom")


def _patch():
    return CodePatch(file_path="a.py", target_content="x", replacement_content="y",
                     root_cause_analysis="rc", explanation="why")


def test_get_vcs_provider_local_by_default(monkeypatch):
    monkeypatch.setenv("VCS_PROVIDER", "local")
    assert isinstance(vp.get_vcs_provider(), vp.LocalVCSProvider)


def test_get_vcs_provider_gitlab(monkeypatch):
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    assert isinstance(vp.get_vcs_provider(), vp.GitLabProvider)


def test_get_vcs_provider_github_routes_to_gitops(monkeypatch):
    monkeypatch.setenv("VCS_PROVIDER", "github")
    monkeypatch.setenv("VCS_REPO_URL", "org/repo")
    from aegis_sre.orchestrator.git_tools import GitOpsProvider
    assert isinstance(vp.get_vcs_provider(), GitOpsProvider)


def test_local_provider_reads_existing_file(tmp_path, monkeypatch):
    f = tmp_path / "f.py"
    f.write_text("hello")
    monkeypatch.chdir(tmp_path)
    prov = vp.LocalVCSProvider()
    assert asyncio.run(prov.fetch_file_content("f.py")) == "hello"
    assert asyncio.run(prov.fetch_file_content("missing.py")) is None


def test_local_provider_pr_is_simulated():
    url = asyncio.run(vp.LocalVCSProvider().create_pull_request(_patch(), TELE))
    assert url == "mock-local-pr-url"


def test_gitlab_provider_without_token_is_mock(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    prov = vp.GitLabProvider("org/repo")
    assert asyncio.run(prov.create_pull_request(_patch(), TELE)) == "mock-gitlab-mr-url"


def test_github_provider_without_token_simulates(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    prov = vp.GitHubProvider("org/repo")
    assert prov.g is None
    assert asyncio.run(prov.create_pull_request(_patch(), TELE)) == "mock-github-pr-url"


def test_github_provider_fetch_without_token_reads_local(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    f = tmp_path / "g.py"
    f.write_text("data")
    monkeypatch.chdir(tmp_path)
    prov = vp.GitHubProvider("org/repo")
    assert asyncio.run(prov.fetch_file_content("g.py")) == "data"
