"""
Sandbox execution engines — the gate that must prove a proposed patch is sound
before it can be deployed.

This module was hardened to close three production-critical defects:

  P0-1  The patch was never applied to the real source. The previous code wrote
        `patch.replacement_content` (the *replacement chunk*, not the whole file)
        out as a standalone file and compiled that fragment — which tells you
        nothing about whether the patch actually fixes the file it targets.
        Fixed: `apply_patch_to_source()` splices target -> replacement into a copy
        of the real file content and the *full patched file* is what gets tested.

  P0-2  Validation was syntax-only (`py_compile` / `node --check`). Syntax passing
        is necessary but not sufficient. Fixed: an optional, operator-trusted
        reproduction command (env `AEGIS_REPRO_COMMAND`) is run against the patched
        workspace so a behavioral regression fails the gate. Reproductions are
        NEVER taken from the (untrusted) crash telemetry — that would be RCE.

  P0-3  E2B failed *open*: a missing `E2B_API_KEY` returned "Mock compilation
        success." Fixed: missing key fails closed, and the default engine selection
        prefers the local engine (which can actually execute) when E2B is not
        configured, so a misconfiguration can never silently pass a patch.
"""

import os
import asyncio
import shlex
from abc import ABC, abstractmethod
from typing import Optional

from aegis_sre.orchestrator.schemas import PatchProposal
from aegis_sre.telemetry.logger import logger

# Compile/repro hard caps so a pathological patch can't hang the swarm.
def _env_int(name: str, default: int) -> int:
    # Guard: a non-int env value must not crash the whole process at import (SB-4).
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


COMPILE_TIMEOUT_SECONDS = _env_int("AEGIS_COMPILE_TIMEOUT", 30)
REPRO_TIMEOUT_SECONDS = _env_int("AEGIS_REPRO_TIMEOUT", 120)


class PatchApplicationError(Exception):
    """The patch's target_content could not be unambiguously located in the source."""


def apply_patch_to_source(patch: PatchProposal, original_source: Optional[str]) -> str:
    """Return the full patched file content.

    - `original_source is None`  -> new-file creation; `replacement_content` *is*
      the file.
    - exactly one occurrence of `target_content` -> replace it.
    - zero occurrences -> the patch does not apply (raise).
    - more than one occurrence -> ambiguous; refuse rather than patch the wrong
      site (an autonomous actor must not guess which match to edit).
    """
    if original_source is None:
        return patch.replacement_content

    target = patch.target_content or ""
    if target == "":
        raise PatchApplicationError(
            "target_content is empty but the source file exists; cannot locate the edit site."
        )

    occurrences = original_source.count(target)
    if occurrences == 0:
        raise PatchApplicationError(
            f"target_content not found in {patch.file_path}; the patch does not apply to the current source."
        )
    if occurrences > 1:
        raise PatchApplicationError(
            f"target_content matches {occurrences} sites in {patch.file_path}; ambiguous, refusing to apply."
        )
    return original_source.replace(target, patch.replacement_content, 1)


class ExecutionEngine(ABC):
    def _compile_argv(self, file_path: str) -> Optional[list]:
        """Compiler argv for a path. None => no validator for this language.

        Returns an argv list (run without a shell) rather than a shell string, so
        a file path can never be interpreted as shell syntax.
        """
        if file_path.endswith(".py"):
            return ["python3", "-m", "py_compile", file_path]
        if file_path.endswith((".js", ".ts")):
            return ["node", "--check", file_path]
        if file_path.endswith(".go"):
            return ["gofmt", "-e", file_path]
        return None

    @abstractmethod
    async def compile_and_test(
        self,
        patch: PatchProposal,
        original_source: Optional[str] = None,
        repro_command: Optional[str] = None,
    ) -> "tuple[bool, str]":
        """Apply the patch to the real source, compile, and (if a trusted repro
        command is provided) run it. Returns (success, output_log)."""


class LocalProcessEngine(ExecutionEngine):
    """Runs compilation/repro as local subprocesses in an isolated temp workspace."""

    async def compile_and_test(
        self,
        patch: PatchProposal,
        original_source: Optional[str] = None,
        repro_command: Optional[str] = None,
    ) -> "tuple[bool, str]":
        import tempfile

        logger.info("provisioning_local_sandbox", node="sandbox", provider="local")

        try:
            patched = apply_patch_to_source(patch, original_source)
        except PatchApplicationError as e:
            logger.error("patch_does_not_apply", node="sandbox", file_path=patch.file_path, error=str(e))
            return False, f"Patch does not apply: {e}"

        with tempfile.TemporaryDirectory(prefix="aegis_sandbox_") as workdir:
            target_path = self._safe_write(workdir, patch.file_path, patched)
            if target_path is None:
                return False, f"Refusing unsafe sandbox path: {patch.file_path}"

            ok, output = await self._compile(target_path)
            if not ok:
                logger.error("compilation_failed", node="sandbox", output=output)
                return False, output

            if repro_command:
                return await self._run_repro(workdir, repro_command)

            logger.info("compilation_success_no_repro", node="sandbox")
            return True, "Patched file compiled. No AEGIS_REPRO_COMMAND set — syntax-only validation."

    @staticmethod
    def _safe_write(workdir: str, rel_path: str, content: str) -> Optional[str]:
        """Write `content` to `workdir/rel_path`, refusing any path that escapes
        the workspace (defence-in-depth on top of the schema's path-traversal check)."""
        target = os.path.normpath(os.path.join(workdir, rel_path))
        if not (target == workdir or target.startswith(workdir + os.sep)):
            logger.warning("sandbox_path_escape_blocked", rel_path=rel_path)
            return None
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return target

    async def _compile(self, path: str) -> "tuple[bool, str]":
        argv = self._compile_argv(path)
        if argv is None:
            # Fail closed: we cannot validate a language we don't understand,
            # so we must not let the patch through.
            return False, f"No compiler available for {os.path.basename(path)}; cannot validate (failing closed)."
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=COMPILE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self._terminate(proc)
            return False, f"Compilation timed out after {COMPILE_TIMEOUT_SECONDS}s."
        except FileNotFoundError:
            return False, f"Compiler for {os.path.basename(path)} is not installed; cannot validate (failing closed)."
        if proc.returncode == 0:
            return True, "Compilation successful."
        return False, (stderr or stdout).decode(errors="replace").strip()

    async def _run_repro(self, workdir: str, command: str) -> "tuple[bool, str]":
        """Run an operator-trusted reproduction command against the patched code."""
        logger.info("running_repro_command", node="sandbox")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=REPRO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self._terminate(proc)
            return False, f"Reproduction command timed out after {REPRO_TIMEOUT_SECONDS}s."
        if proc.returncode == 0:
            return True, "Reproduction passed against the patched code."
        return False, "Reproduction failed against the patched code:\n" + (stderr or stdout).decode(errors="replace").strip()

    @staticmethod
    async def _terminate(proc) -> None:
        """Kill and reap a subprocess that overran its timeout so it can't leak."""
        try:
            proc.kill()
        except ProcessLookupError:
            return  # already exited
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass


class E2BEngine(ExecutionEngine):
    """Runs compilation/repro inside an ephemeral E2B cloud sandbox."""

    async def compile_and_test(
        self,
        patch: PatchProposal,
        original_source: Optional[str] = None,
        repro_command: Optional[str] = None,
    ) -> "tuple[bool, str]":
        logger.info("provisioning_e2b_sandbox", node="sandbox", provider="e2b")

        if not os.environ.get("E2B_API_KEY"):
            # FAIL CLOSED. Previously this returned "Mock compilation success.",
            # which meant a missing secret silently approved every patch.
            logger.error("e2b_api_key_missing_failing_closed", node="sandbox", provider="e2b")
            return False, "E2B_API_KEY not configured; cannot validate patch in E2B (failing closed)."

        try:
            patched = apply_patch_to_source(patch, original_source)
        except PatchApplicationError as e:
            logger.error("patch_does_not_apply", node="sandbox", file_path=patch.file_path, error=str(e))
            return False, f"Patch does not apply: {e}"

        try:
            from e2b import Sandbox

            with Sandbox(id="base") as sandbox:
                sandbox.filesystem.write(patch.file_path, patched)
                argv = self._compile_argv(patch.file_path)
                if argv is None:
                    return False, f"No compiler available for {patch.file_path}; cannot validate (failing closed)."
                # shlex.join quotes each arg so a crafted file_path can't inject
                # shell syntax into the E2B process command (SB-3).
                proc = sandbox.process.start(shlex.join(argv))
                proc.wait()
                if proc.exit_code != 0:
                    return False, str(proc.stderr)

                if repro_command:
                    rproc = sandbox.process.start(repro_command)
                    rproc.wait()
                    if rproc.exit_code != 0:
                        return False, "Reproduction failed against the patched code:\n" + str(rproc.stderr)
                    return True, "Reproduction passed against the patched code."

                return True, "Patched file compiled in E2B. No AEGIS_REPRO_COMMAND set — syntax-only validation."
        except Exception as e:  # noqa: BLE001 - any sandbox error must fail closed
            logger.error("e2b_sandbox_error_failing_closed", node="sandbox", error=str(e))
            return False, f"E2B sandbox error (failing closed): {e}"


def get_sandbox_engine() -> ExecutionEngine:
    """Select the sandbox engine.

    Explicit `SANDBOX_PROVIDER=local|e2b` is honoured. With no explicit choice,
    prefer E2B only when an API key is present; otherwise use the local engine,
    which can actually run — never an E2B engine that would fail closed on every
    call (and previously failed *open*).
    """
    provider = os.environ.get("SANDBOX_PROVIDER", "").lower()
    if provider == "e2b":
        return E2BEngine()
    if provider == "local":
        return LocalProcessEngine()
    if os.environ.get("E2B_API_KEY"):
        return E2BEngine()
    return LocalProcessEngine()
