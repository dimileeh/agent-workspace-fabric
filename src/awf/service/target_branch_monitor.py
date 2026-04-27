"""Target-branch reconciliation monitor.

This is the branch-level counterpart to workspace PR monitors. Workspace
monitors make individual PRs healthy; this monitor inspects the integrated
target branch after merges and applies deterministic follow-up repairs that
only make sense after multiple PRs have landed together. The first resolver is
Python/Alembic-specific: merge multiple Alembic heads into a follow-up commit.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.service.alembic_resolver import (
    AlembicMergeResolver,
    AlembicResolveResult,
)


class BranchResolver(Protocol):
    """Resolver that can mutate a target-branch checkout if it finds an issue."""

    def resolve(self, repo_path: Path) -> AlembicResolveResult: ...


class TargetBranchMonitorStatus(StrEnum):
    """Outcome of one target-branch reconciliation pass."""

    clean = "clean"
    would_commit = "would_commit"
    committed = "committed"


@dataclass(frozen=True)
class TargetBranchMonitorResult:
    """Structured result for API/CLI/log/event payloads."""

    repo_url: str
    branch: str
    checkout_path: Path
    status: TargetBranchMonitorStatus
    resolver_results: tuple[AlembicResolveResult, ...]
    commit_sha: str | None = None
    pushed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_url": self.repo_url,
            "branch": self.branch,
            "checkout_path": str(self.checkout_path),
            "status": self.status.value,
            "resolver_results": [result.to_dict() for result in self.resolver_results],
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
        }


class TargetBranchMonitorError(RuntimeError):
    """Raised when target-branch reconciliation cannot safely continue."""

    def __init__(self, *, operation: str, result: CommandResult) -> None:
        self.operation = operation
        self.result = result
        super().__init__(
            f"{operation} failed (exit={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


class TargetBranchReconcileMonitor:
    """Checks out a target branch, runs branch-level resolvers, and pushes repairs."""

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        work_dir: Path,
        resolvers: Sequence[BranchResolver] | None = None,
    ) -> None:
        self._runner = runner
        self._work_dir = work_dir.expanduser().resolve()
        self._resolvers: tuple[BranchResolver, ...] = (
            tuple(resolvers) if resolvers is not None else (AlembicMergeResolver(),)
        )

    async def reconcile(
        self,
        *,
        repo_url: str,
        branch: str,
        dry_run: bool = False,
    ) -> TargetBranchMonitorResult:
        """Run one reconciliation pass for ``repo_url``/``branch``."""

        checkout_path = self._checkout_path(repo_url=repo_url, branch=branch)
        await self._prepare_checkout(
            repo_url=repo_url,
            branch=branch,
            checkout_path=checkout_path,
        )

        resolver_results = tuple(resolver.resolve(checkout_path) for resolver in self._resolvers)
        changed_paths = tuple(
            result.generated_path
            for result in resolver_results
            if result.changed and result.generated_path is not None
        )
        if not changed_paths:
            return TargetBranchMonitorResult(
                repo_url=repo_url,
                branch=branch,
                checkout_path=checkout_path,
                status=TargetBranchMonitorStatus.clean,
                resolver_results=resolver_results,
            )
        if dry_run:
            return TargetBranchMonitorResult(
                repo_url=repo_url,
                branch=branch,
                checkout_path=checkout_path,
                status=TargetBranchMonitorStatus.would_commit,
                resolver_results=resolver_results,
            )

        await self._git(
            ["add", "--", *(str(path.relative_to(checkout_path)) for path in changed_paths)],
            cwd=checkout_path,
            operation="target_branch.git_add",
        )
        staged = await self._runner.run(
            ["git", "-C", str(checkout_path), "diff", "--cached", "--quiet"]
        )
        if staged.returncode == 0:
            return TargetBranchMonitorResult(
                repo_url=repo_url,
                branch=branch,
                checkout_path=checkout_path,
                status=TargetBranchMonitorStatus.clean,
                resolver_results=resolver_results,
            )
        if staged.returncode != 1:
            raise TargetBranchMonitorError(
                operation="target_branch.git_diff_cached",
                result=staged,
            )

        await self._git(
            [
                "commit",
                "-m",
                f"fix(migrations): merge Alembic heads on {branch}",
                "-m",
                (
                    "AWF detected multiple Alembic heads after integrating "
                    "parallel workspace PRs and generated a merge revision."
                ),
            ],
            cwd=checkout_path,
            operation="target_branch.git_commit",
        )
        commit = await self._git(
            ["rev-parse", "HEAD"],
            cwd=checkout_path,
            operation="target_branch.rev_parse",
        )
        await self._git(
            ["push", "origin", f"HEAD:{branch}"],
            cwd=checkout_path,
            operation="target_branch.git_push",
        )
        return TargetBranchMonitorResult(
            repo_url=repo_url,
            branch=branch,
            checkout_path=checkout_path,
            status=TargetBranchMonitorStatus.committed,
            resolver_results=resolver_results,
            commit_sha=commit.stdout.strip() or None,
            pushed=True,
        )

    async def _prepare_checkout(
        self,
        *,
        repo_url: str,
        branch: str,
        checkout_path: Path,
    ) -> None:
        checkout_path.parent.mkdir(parents=True, exist_ok=True)
        if checkout_path.exists() and not (checkout_path / ".git").exists():
            raise RuntimeError(
                f"target branch checkout path exists but is not a git checkout: {checkout_path}"
            )
        if (checkout_path / ".git").exists():
            await self._git(
                ["fetch", "origin", branch, "--prune"],
                cwd=checkout_path,
                operation="target_branch.git_fetch",
            )
            await self._git(
                ["checkout", branch],
                cwd=checkout_path,
                operation="target_branch.git_checkout",
            )
            await self._git(
                ["reset", "--hard", f"origin/{branch}"],
                cwd=checkout_path,
                operation="target_branch.git_reset",
            )
            await self._git(
                ["clean", "-fd"],
                cwd=checkout_path,
                operation="target_branch.git_clean",
            )
            return

        await self._run(
            [
                "git",
                "clone",
                "--branch",
                branch,
                "--single-branch",
                repo_url,
                str(checkout_path),
            ],
            operation="target_branch.git_clone",
        )

    async def _git(
        self,
        args: list[str],
        *,
        cwd: Path,
        operation: str,
    ) -> CommandResult:
        return await self._run(["git", "-C", str(cwd), *args], operation=operation)

    async def _run(self, args: list[str], *, operation: str) -> CommandResult:
        result = await self._runner.run(args)
        if not result.ok:
            raise TargetBranchMonitorError(operation=operation, result=result)
        return result

    def _checkout_path(self, *, repo_url: str, branch: str) -> Path:
        slug = _slugify_repo(repo_url)
        digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
        branch_slug = _slugify_branch(branch)
        return self._work_dir / "target-branches" / f"{slug}-{digest}-{branch_slug}"


async def run_target_branch_reconcile_once(
    *,
    runner: AsyncCommandRunner,
    work_dir: Path,
    repo_url: str,
    branch: str,
    dry_run: bool = False,
) -> TargetBranchMonitorResult:
    """Convenience entry point for CLIs and service hooks."""

    monitor = TargetBranchReconcileMonitor(runner=runner, work_dir=work_dir)
    return await monitor.reconcile(repo_url=repo_url, branch=branch, dry_run=dry_run)


_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slugify_repo(repo_url: str) -> str:
    tail = repo_url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _SLUG_RE.sub("-", tail) or "repo"


def _slugify_branch(branch: str) -> str:
    return _SLUG_RE.sub("-", branch).strip("-") or "branch"
