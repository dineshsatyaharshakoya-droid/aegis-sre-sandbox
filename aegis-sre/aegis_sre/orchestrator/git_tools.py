"""
Git integration module — the agent's hands for GitOps remediation.

The repair swarm produces a `PatchProposal`; after a human approves it
(`core/approvals.py` -> `create_pull_request`), this module turns that proposal
into a real pull request by driving a local git clone:

    clone (shallow) -> branch -> apply patch -> commit -> push -> open PR

Why a real clone instead of the GitHub Contents API: the Contents API path wrote
`replacement_content` as the *whole file*, which silently truncated every file to
the replacement chunk (the same fragment-vs-file defect P0-1 fixed in the
sandbox). Cloning lets us splice the chunk into the real file with the audited
`apply_patch_to_source()` helper, so what we push is byte-for-byte what the
sandbox validated.

Design notes:
  * git runs as a subprocess (no GitPython dependency); the only new requirement
    is the `git` binary, which every CI/runtime image already ships.
  * The token is injected into the remote URL only in-memory and is redacted from
    every log line and error message (`_redact`). The authed remote lives in the
    temp clone's `.git/config`, which is deleted in a `finally`.
  * The PR step uses PyGithub (already a dependency). The client is injectable so
    the clone/branch/commit/push half can be tested end-to-end against a local
    bare repo with no network and no token.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

from aegis_sre.orchestrator.schemas import PatchProposal, TelemetryEvent
from aegis_sre.orchestrator.sandbox_engine import (
    PatchApplicationError,
    apply_patch_to_source,
)
from aegis_sre.orchestrator.vcs_provider import GitHubProvider
from aegis_sre.telemetry.logger import logger


class GitToolError(RuntimeError):
    """A git operation failed (clone/branch/commit/push) or the patch was a no-op."""


# --- URL / token helpers ---------------------------------------------------

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)?(?P<slug>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)


def _normalize_repo(repo_url: str) -> tuple[str, str]:
    """Return `(owner/repo, clone_url)` from any accepted repo reference.

    Accepts `owner/repo`, `owner/repo.git`, `https://github.com/owner/repo(.git)`,
    and `git@github.com:owner/repo.git`. Also accepts a local/`file://` remote
    (self-hosted git or tests): the clone URL is passed through unchanged and the
    slug is the basename — the API slug is only used by the PR step, which is
    skipped/injected for non-GitHub remotes.
    """
    ref = repo_url.strip()
    if ref.startswith(("file://", "/", "./", "../")):
        slug = re.sub(r"\.git/?$", "", ref.rstrip("/").rsplit("/", 1)[-1])
        return slug, ref
    m = _GITHUB_URL_RE.match(ref)
    if not m:
        raise GitToolError(f"Unrecognized GitHub repo reference: {repo_url!r}")
    slug = m.group("slug")
    return slug, f"https://github.com/{slug}.git"


def _authed_url(https_url: str, token: str) -> str:
    """Embed the token for HTTPS auth. `x-access-token` works for both classic and
    fine-grained PATs."""
    return https_url.replace("https://", f"https://x-access-token:{token}@", 1)


def _redact(text: str, token: Optional[str]) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    # Belt-and-braces: strip any `user:secret@` that slipped into a URL.
    return re.sub(r"https://[^@/\s]+:[^@/\s]+@", "https://***@", text)


# --- low-level git -----------------------------------------------------------


async def _run_git(
    args: list[str], cwd: Optional[str] = None, token: Optional[str] = None
) -> str:
    """Run `git <args>` and return stdout. Raises `GitToolError` (token-redacted)
    on a non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Never block on a credential prompt — fail fast instead of hanging.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = _redact((err or out).decode("utf-8", "replace").strip(), token)
        safe_args = _redact(" ".join(args), token)
        raise GitToolError(f"git {safe_args} failed (exit {proc.returncode}): {detail}")
    return stdout


# --- the engine --------------------------------------------------------------


@dataclass
class GitTools:
    """Stateless GitOps engine for one repository.

    A single `submit_patch()` performs the whole clone->PR flow in an isolated
    temp workspace and always cleans it up. The intermediate steps are public so
    they can be driven (and tested) individually.
    """

    repo_url: str
    token: Optional[str] = None
    base_branch: Optional[str] = None  # None => the repo's default branch
    author_name: str = "Aegis SRE"
    author_email: str = "aegis-sre@users.noreply.github.com"
    github_client: object = None  # injectable PyGithub client for the PR step

    def __post_init__(self) -> None:
        self.slug, self.https_url = _normalize_repo(self.repo_url)

    # -- step 1: clone --------------------------------------------------------
    async def clone(self) -> str:
        """Shallow-clone into a fresh temp dir and return its path. Records the
        resolved base branch on `self.base_branch`."""
        workdir = tempfile.mkdtemp(prefix="aegis-gitops-")
        url = _authed_url(self.https_url, self.token) if self.token else self.https_url
        args = ["clone", "--depth", "1"]
        if self.base_branch:
            args += ["--branch", self.base_branch]
        args += [url, workdir]
        try:
            await _run_git(args, token=self.token)
            if not self.base_branch:
                self.base_branch = (
                    await _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workdir)
                ).strip()
            # Identity for the commit we are about to make.
            await _run_git(["config", "user.name", self.author_name], cwd=workdir)
            await _run_git(["config", "user.email", self.author_email], cwd=workdir)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        logger.info("gitops_cloned", repo=self.slug, base=self.base_branch)
        return workdir

    # -- step 2: branch -------------------------------------------------------
    async def create_branch(self, workdir: str, branch: str) -> None:
        await _run_git(["checkout", "-b", branch], cwd=workdir)
        logger.info("gitops_branched", repo=self.slug, branch=branch)

    # -- step 3: apply the patch ---------------------------------------------
    def apply_patch(self, workdir: str, patch: PatchProposal) -> None:
        """Splice the patch into the real file on disk using the audited helper.

        `file_path` is schema-validated relative + traversal-free; we re-check the
        resolved path stays inside `workdir` as defense in depth.
        """
        target = os.path.normpath(os.path.join(workdir, patch.file_path))
        if os.path.commonpath([os.path.realpath(workdir), os.path.realpath(target)]) != os.path.realpath(workdir):
            raise GitToolError(f"Refusing to write outside the repo: {patch.file_path!r}")

        original = None
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as f:
                original = f.read()
        try:
            patched = apply_patch_to_source(patch, original)
        except PatchApplicationError as e:
            raise GitToolError(f"Patch does not apply to {patch.file_path}: {e}") from e

        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(patched)
        logger.info("gitops_patched", repo=self.slug, file=patch.file_path)

    # -- step 4: commit + push ------------------------------------------------
    async def commit_and_push(
        self, workdir: str, branch: str, message: str, file_path: str
    ) -> None:
        await _run_git(["add", "--", file_path], cwd=workdir)
        # A patch that resolves to the existing content must not open an empty PR.
        if not (await _run_git(["status", "--porcelain"], cwd=workdir)).strip():
            raise GitToolError("Patch produced no change to the file; nothing to commit.")
        await _run_git(["commit", "-m", message], cwd=workdir)
        await _run_git(["push", "--set-upstream", "origin", branch], cwd=workdir, token=self.token)
        logger.info("gitops_pushed", repo=self.slug, branch=branch)

    # -- step 5: open the PR --------------------------------------------------
    async def open_pull_request(self, branch: str, title: str, body: str) -> str:
        client = self.github_client
        if client is None:
            if not self.token:
                raise GitToolError("No GitHub token available to open a pull request.")
            from github import Auth, Github

            client = Github(auth=Auth.Token(self.token))
        repo = await asyncio.to_thread(client.get_repo, self.slug)
        pr = await asyncio.to_thread(
            repo.create_pull, title=title, body=body, head=branch, base=self.base_branch
        )
        logger.info("gitops_pr_opened", repo=self.slug, url=pr.html_url)
        return pr.html_url

    # -- orchestration --------------------------------------------------------
    @staticmethod
    def branch_name(telemetry: TelemetryEvent) -> str:
        svc = re.sub(r"[^a-zA-Z0-9._-]+", "-", telemetry.service_name or "service").strip("-")
        return f"aegis/fix-{svc}-{telemetry.event_id[:8]}"

    async def submit_patch(self, patch: PatchProposal, telemetry: TelemetryEvent) -> str:
        """Full flow: clone -> branch -> apply -> commit -> push -> PR. Returns the
        PR URL. The temp workspace is always removed."""
        workdir = await self.clone()
        try:
            branch = self.branch_name(telemetry)
            await self.create_branch(workdir, branch)
            self.apply_patch(workdir, patch)
            commit_msg = (
                f"[Aegis] Auto-fix {telemetry.service_name} crash in {patch.file_path}\n\n"
                f"Root cause: {patch.root_cause_analysis}\n"
                f"Incident: {telemetry.event_id}"
            )
            await self.commit_and_push(workdir, branch, commit_msg, patch.file_path)
            title = f"[Aegis] Auto-fix {telemetry.service_name} crash in {patch.file_path}"
            body = (
                "## Aegis Autonomous SRE Fix\n\n"
                f"**Incident:** `{telemetry.event_id}`\n"
                f"**Service:** {telemetry.service_name}\n"
                f"**File:** `{patch.file_path}`\n\n"
                f"**Root cause analysis**\n{patch.root_cause_analysis}\n\n"
                f"**Explanation**\n{patch.explanation}\n\n"
                "_This patch was validated in the sandbox and approved by a human "
                "operator before this PR was opened._"
            )
            return await self.open_pull_request(branch, title, body)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# --- VCS provider adapter (the action-loop entry point) ----------------------


class GitOpsProvider(GitHubProvider):
    """GitHub provider whose `create_pull_request` uses the clone-based GitOps
    flow above. Inherits the API-based `fetch_file_content` so the researcher node
    keeps reading source over the API.
    """

    async def create_pull_request(
        self, patch: PatchProposal, telemetry: TelemetryEvent
    ) -> str:
        if not self.token:
            # No credentials -> stay in simulation mode rather than failing the
            # approval (matches the other providers' mock behavior).
            logger.info("simulating_pull_request", provider="gitops", repo=self.repo_url)
            return "mock-github-pr-url"
        tools = GitTools(self.repo_url, token=self.token, github_client=self.g)
        return await tools.submit_patch(patch, telemetry)
