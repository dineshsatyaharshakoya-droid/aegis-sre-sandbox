import os
import asyncio
from abc import ABC, abstractmethod
from github import Github, Auth
from aegis_sre.orchestrator.schemas import PatchProposal, TelemetryEvent
from aegis_sre.telemetry.logger import logger

class VCSProvider(ABC):
    @abstractmethod
    async def fetch_file_content(self, file_path: str) -> str | None:
        """Fetch file content from the version control system."""
        pass
        
    @abstractmethod
    async def create_pull_request(self, patch: PatchProposal, telemetry: TelemetryEvent) -> str:
        """Create a pull request with the generated patch."""
        pass

class GitHubProvider(VCSProvider):
    def __init__(self, repo_url: str):
        self.repo_url = repo_url
        self.token = os.environ.get("GITHUB_TOKEN")
        if self.token:
            auth = Auth.Token(self.token)
            self.g = Github(auth=auth)
        else:
            self.g = None
        
    async def fetch_file_content(self, file_path: str) -> str | None:
        if not self.g:
            logger.info("fetching_file_mock", provider="github", repo=self.repo_url, file_path=file_path)
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    return f.read()
            return None
            
        try:
            # Run blocking PyGithub calls in a thread pool
            repo = await asyncio.to_thread(self.g.get_repo, self.repo_url)
            contents = await asyncio.to_thread(repo.get_contents, file_path)
            return contents.decoded_content.decode("utf-8")
        except Exception as e:
            logger.error("github_api_error", error=str(e))
            return None

    async def create_pull_request(self, patch: PatchProposal, telemetry: TelemetryEvent) -> str:
        if not self.g:
            logger.info("simulating_pull_request", provider="github", repo=self.repo_url)
            return "mock-github-pr-url"
            
        try:
            repo = await asyncio.to_thread(self.g.get_repo, self.repo_url)
            source_branch = repo.default_branch
            main_ref = await asyncio.to_thread(repo.get_git_ref, f"heads/{source_branch}")
            
            new_branch_name = f"aegis-fix-{telemetry.event_id[:8]}"
            await asyncio.to_thread(repo.create_git_ref, ref=f"refs/heads/{new_branch_name}", sha=main_ref.object.sha)
            
            contents = await asyncio.to_thread(repo.get_contents, patch.file_path, ref=source_branch)
            commit_message = f"[Aegis] Auto-fix for {telemetry.service_name} crash"

            # Splice the patch into the FULL current file (audit #12 fix): writing
            # patch.replacement_content directly truncated the file to the chunk
            # (the P0-1 bug). apply_patch_to_source applies the chunk in context.
            from aegis_sre.orchestrator.sandbox_engine import apply_patch_to_source
            current_source = contents.decoded_content.decode("utf-8")
            patched_source = apply_patch_to_source(patch, current_source)

            await asyncio.to_thread(
                repo.update_file,
                contents.path,
                commit_message,
                patched_source,
                contents.sha,
                branch=new_branch_name
            )
            
            pr_body = f"## Aegis Autonomous SRE Fix\n**Root Cause:** {patch.root_cause_analysis}\n**Explanation:** {patch.explanation}"
            pr = await asyncio.to_thread(
                repo.create_pull,
                title=commit_message,
                body=pr_body,
                head=new_branch_name,
                base=source_branch
            )
            
            logger.info("pull_request_created", url=pr.html_url)
            return pr.html_url
        except Exception as e:
            logger.error("github_pr_error", error=str(e))
            return "mock-github-pr-url"

class GitLabProvider(VCSProvider):
    def __init__(self, repo_url: str):
        self.repo_url = repo_url
        self.token = os.environ.get("GITLAB_TOKEN")
        
    async def fetch_file_content(self, file_path: str) -> str | None:
        logger.info("fetching_file", provider="gitlab", repo=self.repo_url, file_path=file_path)
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return f.read()
        return None

    async def create_pull_request(self, patch: PatchProposal, telemetry: TelemetryEvent) -> str:
        # Honest failure (audit #11): the GitLab API path was never implemented and
        # previously returned a hardcoded fake MR URL, silently pretending to work.
        # Fail loudly so nobody mistakes a mock for a real MR; use VCS_PROVIDER=github.
        if not self.token:
            logger.info("simulating_pull_request", provider="gitlab", repo=self.repo_url)
            return "mock-gitlab-mr-url"
        raise NotImplementedError(
            "GitLabProvider.create_pull_request is not implemented. Use VCS_PROVIDER=github "
            "(clone-based GitOps), or implement the GitLab MR API here.")

class LocalVCSProvider(VCSProvider):
    """Fallback for purely local testing without cloud VCS"""
    async def fetch_file_content(self, file_path: str) -> str | None:
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return f.read()
        return None

    async def create_pull_request(self, patch: PatchProposal, telemetry: TelemetryEvent) -> str:
        logger.info("simulating_pull_request", provider="local", title=f"[Aegis Auto-Fix] Resolved CrashLoop in {telemetry.service_name}", diff=patch.file_path)
        return "mock-local-pr-url"

def get_vcs_provider() -> VCSProvider:
    provider_name = os.environ.get("VCS_PROVIDER", "local").lower()
    repo_url = os.environ.get("VCS_REPO_URL", "org/repo")
    
    if provider_name == "github":
        # Clone-based GitOps flow (clone -> branch -> apply -> commit -> push -> PR).
        # Lazy import so the module stays importable without git_tools' deps.
        from aegis_sre.orchestrator.git_tools import GitOpsProvider
        return GitOpsProvider(repo_url)
    elif provider_name == "gitlab":
        return GitLabProvider(repo_url)
    else:
        return LocalVCSProvider()
