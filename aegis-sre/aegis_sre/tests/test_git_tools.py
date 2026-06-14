"""
End-to-end test for the GitOps engine (`orchestrator/git_tools.py`).

No network and no real token: a local *bare* repo stands in for the GitHub
remote, cloned over a `file://` URL. The clone -> branch -> apply -> commit ->
push half runs for real against git; only the PR step (a GitHub API call) is
faked via an injected client. After `submit_patch` we re-clone the bare repo and
assert the pushed branch contains the correctly spliced file.
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from aegis_sre.orchestrator.git_tools import GitToolError, GitTools, _normalize_repo, _redact
from aegis_sre.orchestrator.schemas import PatchProposal, TelemetryEvent


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote_repo(tmp_path):
    """A bare repo (the 'remote') seeded with one commit on `main` containing
    main.py. Returns (bare_path, file:// url)."""
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "seed@test", cwd=work)
    _git("config", "user.name", "seed", cwd=work)
    (work / "main.py").write_text("def process(data):\n    return data['k']\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "seed", cwd=work)

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return bare, f"file://{bare}"


class _FakePR:
    html_url = "https://github.com/acme/sandbox/pull/1"


class _FakeRepo:
    def __init__(self):
        self.kwargs = None

    def create_pull(self, **kwargs):
        self.kwargs = kwargs
        return _FakePR()


class _FakeGithub:
    def __init__(self):
        self.repo = _FakeRepo()

    def get_repo(self, slug):
        self.slug = slug
        return self.repo


PATCH = PatchProposal(
    file_path="main.py",
    target_content="    return data['k']",
    replacement_content="    if not data:\n        return None\n    return data.get('k')",
    root_cause_analysis="KeyError when data is empty at main.py.",
    explanation="Guard against empty/missing key.",
)
TELEMETRY = TelemetryEvent(event_id="abcd1234ef", service_name="payments", crash_log="KeyError: 'k'")


def test_normalize_repo_accepts_many_forms():
    for ref in ["acme/sandbox", "acme/sandbox.git", "https://github.com/acme/sandbox",
                "https://github.com/acme/sandbox.git", "git@github.com:acme/sandbox.git"]:
        slug, url = _normalize_repo(ref)
        assert slug == "acme/sandbox"
        assert url == "https://github.com/acme/sandbox.git"


def test_redact_strips_token_and_userinfo():
    assert "secret" not in _redact("clone https://x:secret@github.com/a/b failed", "secret")
    assert "***" in _redact("https://x-access-token:abc@github.com/a/b", None)


def test_submit_patch_pushes_branch_and_opens_pr(remote_repo, tmp_path):
    bare, url = remote_repo
    fake = _FakeGithub()
    tools = GitTools(repo_url=url, base_branch="main", github_client=fake)

    pr_url = asyncio.run(tools.submit_patch(PATCH, TELEMETRY))

    # PR step received the right branch/base.
    assert pr_url == "https://github.com/acme/sandbox/pull/1"
    assert fake.repo.kwargs["head"] == "aegis/fix-payments-abcd1234"
    assert fake.repo.kwargs["base"] == "main"

    # The branch really landed on the remote with the spliced file.
    verify = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "aegis/fix-payments-abcd1234", str(bare), str(verify)],
        check=True, capture_output=True,
    )
    patched = (verify / "main.py").read_text()
    assert "data.get('k')" in patched
    assert "if not data:" in patched
    # The rest of the original file survived (not truncated to the chunk).
    assert patched.startswith("def process(data):")


def test_empty_patch_does_not_commit(remote_repo):
    """A patch whose replacement equals the original must not open an empty PR."""
    bare, url = remote_repo
    noop = PatchProposal(
        file_path="main.py",
        target_content="    return data['k']",
        replacement_content="    return data['k']",  # identical -> no diff
        root_cause_analysis="n/a", explanation="n/a",
    )
    tools = GitTools(repo_url=url, base_branch="main", github_client=_FakeGithub())
    with pytest.raises(GitToolError, match="nothing to commit"):
        asyncio.run(tools.submit_patch(noop, TELEMETRY))


def test_apply_patch_rejects_path_escape(remote_repo, tmp_path):
    bare, url = remote_repo
    tools = GitTools(repo_url=url, base_branch="main", github_client=_FakeGithub())
    workdir = asyncio.run(tools.clone())
    # file_path bypasses the schema validator by constructing the object loosely.
    escape = PATCH.model_copy(update={"file_path": "main.py"})
    object.__setattr__(escape, "file_path", "../escape.py")
    with pytest.raises(GitToolError, match="outside the repo"):
        tools.apply_patch(workdir, escape)
