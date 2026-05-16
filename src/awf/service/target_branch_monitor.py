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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.logging import get_logger
from awf.service.alembic_resolver import (
    AlembicMergeResolver,
    AlembicResolveResult,
)
from awf.service.staleness import (
    StalenessFinding,
    StalenessRefreshService,
    TargetBranchState,
    TargetBranchStateProvider,
)

_log = get_logger(__name__)


class BranchResolver(Protocol):
    """Resolver that can mutate a target-branch checkout if it finds an issue."""

    def resolve(self, repo_path: Path) -> AlembicResolveResult: ...


class TargetBranchMonitorStatus(StrEnum):
    """Outcome of one target-branch reconciliation pass."""

    clean = "clean"
    would_commit = "would_commit"
    policy_blocked = "policy_blocked"
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
    changed_paths: tuple[str, ...] = ()
    dry_run: bool = False
    commit_allowed: bool = True
    policy_reason_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_url": self.repo_url,
            "branch": self.branch,
            "checkout_path": str(self.checkout_path),
            "status": self.status.value,
            "resolver_results": [result.to_dict() for result in self.resolver_results],
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
            "changed_paths": list(self.changed_paths),
            "dry_run": self.dry_run,
            "commit_allowed": self.commit_allowed,
            "policy_reason_code": self.policy_reason_code,
        }


@dataclass(frozen=True)
class CandidateRefreshSummary:
    """Per-candidate staleness refresh outcome."""

    candidate_id: str
    workspace_id: str
    stale: bool
    findings_count: int
    stale_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "workspace_id": self.workspace_id,
            "stale": self.stale,
            "findings_count": self.findings_count,
            "stale_reason": self.stale_reason,
            "error": self.error,
        }


@dataclass(frozen=True)
class ReconcileAndRefreshResult:
    """Combined result of target-branch reconciliation + staleness refresh."""

    reconcile: TargetBranchMonitorResult
    candidate_refreshes: tuple[CandidateRefreshSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "reconcile": self.reconcile.to_dict(),
            "candidate_refreshes": [s.to_dict() for s in self.candidate_refreshes],
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
        allow_commits: bool = True,
    ) -> None:
        self._runner = runner
        self._work_dir = work_dir.expanduser().resolve()
        self._resolvers: tuple[BranchResolver, ...] = (
            tuple(resolvers) if resolvers is not None else (AlembicMergeResolver(),)
        )
        self._allow_commits = allow_commits

    async def reconcile(
        self,
        *,
        repo_url: str,
        branch: str,
        dry_run: bool = False,
    ) -> TargetBranchMonitorResult:
        """Run one reconciliation pass for ``repo_url``/``branch``."""

        checkout_path = self.checkout_path(repo_url=repo_url, branch=branch)
        await self._prepare_checkout(
            repo_url=repo_url,
            branch=branch,
            checkout_path=checkout_path,
        )

        resolver_results = tuple(resolver.resolve(checkout_path) for resolver in self._resolvers)
        changed_paths = tuple(
            _generated_relative_path(result, checkout_path)
            for result in resolver_results
            if result.changed
        )
        if not changed_paths:
            return TargetBranchMonitorResult(
                repo_url=repo_url,
                branch=branch,
                checkout_path=checkout_path,
                status=TargetBranchMonitorStatus.clean,
                resolver_results=resolver_results,
                dry_run=dry_run,
                commit_allowed=self._allow_commits,
            )
        if dry_run:
            return TargetBranchMonitorResult(
                repo_url=repo_url,
                branch=branch,
                checkout_path=checkout_path,
                status=TargetBranchMonitorStatus.would_commit,
                resolver_results=resolver_results,
                changed_paths=changed_paths,
                dry_run=True,
                commit_allowed=self._allow_commits,
                policy_reason_code="TARGET_BRANCH_DRY_RUN",
            )
        if not self._allow_commits:
            return TargetBranchMonitorResult(
                repo_url=repo_url,
                branch=branch,
                checkout_path=checkout_path,
                status=TargetBranchMonitorStatus.policy_blocked,
                resolver_results=resolver_results,
                changed_paths=changed_paths,
                commit_allowed=False,
                policy_reason_code="TARGET_BRANCH_COMMIT_POLICY_DENIED",
            )

        await self._git(
            ["add", "--", *changed_paths],
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
                changed_paths=changed_paths,
                commit_allowed=self._allow_commits,
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
            changed_paths=changed_paths,
            commit_allowed=self._allow_commits,
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

    def checkout_path(self, *, repo_url: str, branch: str) -> Path:
        slug = _slugify_repo(repo_url)
        digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
        branch_slug = _slugify_branch(branch)
        return self._work_dir / "target-branches" / f"{slug}-{digest}-{branch_slug}"


class GitCheckoutTargetBranchStateProvider(TargetBranchStateProvider):
    """Builds ``TargetBranchState`` from a local git checkout using git commands.

    Expects the checkout to already be at the latest target HEAD (e.g. after
    ``TargetBranchReconcileMonitor.reconcile()`` has run).  Uses an injected
    ``AsyncCommandRunner`` so it is testable with ``FakeCommandRunner``.
    """

    def __init__(
        self,
        *,
        runner: AsyncCommandRunner,
        checkout_path: Path,
    ) -> None:
        self._runner = runner
        self._checkout_path = checkout_path
        self._head_sha: str | None = None

    async def fetch(
        self,
        *,
        repo_url: str,  # noqa: ARG002 — part of the Protocol signature
        branch: str,
        base_sha: str,
    ) -> TargetBranchState:
        if self._head_sha is None:
            head_result = await self._run_git(
                ["rev-parse", "HEAD"],
                operation="target_branch_state.rev_parse",
            )
            self._head_sha = head_result.stdout.strip()
        head_sha = self._head_sha

        count_result = await self._run_git(
            ["rev-list", "--count", f"{base_sha}..HEAD"],
            operation="target_branch_state.rev_list",
        )
        advanced_commits = int(count_result.stdout.strip())

        diff_result = await self._run_git(
            ["diff", "--name-only", f"{base_sha}..HEAD"],
            operation="target_branch_state.diff_name_only",
        )
        changed_paths = tuple(line for line in diff_result.stdout.strip().split("\n") if line)

        return TargetBranchState(
            branch=branch,
            head_sha=head_sha,
            changed_paths=changed_paths,
            advanced_commits=advanced_commits,
        )

    async def _run_git(
        self,
        args: list[str],
        *,
        operation: str,
    ) -> CommandResult:
        result = await self._runner.run(
            ["git", "-C", str(self._checkout_path), *args],
        )
        if not result.ok:
            raise TargetBranchMonitorError(operation=operation, result=result)
        return result


async def reconcile_and_refresh_stale_candidates(
    *,
    reconcile_fn: Callable[..., Awaitable[TargetBranchMonitorResult]],
    repo_url: str,
    branch: str,
    session_factory: async_sessionmaker[AsyncSession],
    target_state_for_base_sha: Callable[[str], Awaitable[TargetBranchState]],
    exclude_workspace_ids: set[str] | None = None,
    dry_run: bool = False,
) -> ReconcileAndRefreshResult:
    """Run target-branch reconciliation, then refresh staleness for open candidates.

    1. Calls ``reconcile_fn`` (typically ``TargetBranchReconcileMonitor.reconcile``).
    2. If reconciliation succeeds, opens a DB session and queries all open merge
       candidates for the same ``repo_url``/``base_branch``.
    3. For each open candidate (excluding ``exclude_workspace_ids``), calls
       ``StalenessRefreshService.refresh_candidate()`` with the appropriate
       ``TargetBranchState`` supplied by ``target_state_for_base_sha``.
    4. Individual candidate refresh failures are recorded in the result but do
       not prevent other candidates from being refreshed or hide the successful
       reconciliation.
    5. Returns ``ReconcileAndRefreshResult`` with both the reconciliation
       outcome and per-candidate summaries.
    """

    reconcile_result = await reconcile_fn(
        repo_url=repo_url,
        branch=branch,
        dry_run=dry_run,
    )

    excluded = exclude_workspace_ids or set()

    summaries: list[CandidateRefreshSummary] = []
    async with session_factory() as session:
        from awf.db.repositories import MergeCandidateRepository

        mc_repo = MergeCandidateRepository(session)
        candidates = await mc_repo.list_queue(
            repo_url=repo_url,
            base_branch=branch,
        )

        service = StalenessRefreshService(session)
        target_states: dict[str, TargetBranchState] = {}

        for candidate in candidates:
            if candidate.workspace_id in excluded:
                continue

            try:
                if candidate.base_sha is None:
                    summaries.append(
                        CandidateRefreshSummary(
                            candidate_id=candidate.id,
                            workspace_id=candidate.workspace_id,
                            stale=candidate.stale,
                            stale_reason=candidate.stale_reason,
                            findings_count=0,
                        )
                    )
                    continue

                if candidate.base_sha not in target_states:
                    target_states[candidate.base_sha] = await target_state_for_base_sha(
                        candidate.base_sha
                    )
                target = target_states[candidate.base_sha]

                refresh_result = await service.refresh_candidate(
                    candidate.id,
                    target=target,
                )
                summaries.append(
                    CandidateRefreshSummary(
                        candidate_id=candidate.id,
                        workspace_id=candidate.workspace_id,
                        stale=refresh_result.stale,
                        stale_reason=_summary_stale_reason(
                            refresh_result.findings,
                            fallback=candidate.stale_reason,
                        ),
                        findings_count=len(refresh_result.findings),
                    )
                )
            except Exception as exc:
                _log.warning(
                    "target_branch.candidate_refresh_failed",
                    candidate_id=candidate.id,
                    workspace_id=candidate.workspace_id,
                    error=str(exc)[:500],
                )
                summaries.append(
                    CandidateRefreshSummary(
                        candidate_id=candidate.id,
                        workspace_id=candidate.workspace_id,
                        stale=candidate.stale,
                        stale_reason=candidate.stale_reason,
                        findings_count=0,
                        error=str(exc)[:1000],
                    )
                )

        await session.commit()

    return ReconcileAndRefreshResult(
        reconcile=reconcile_result,
        candidate_refreshes=tuple(summaries),
    )


async def run_target_branch_reconcile_once(
    *,
    runner: AsyncCommandRunner,
    work_dir: Path,
    repo_url: str,
    branch: str,
    dry_run: bool = False,
    allow_commits: bool = True,
) -> TargetBranchMonitorResult:
    """Convenience entry point for CLIs and service hooks."""

    monitor = TargetBranchReconcileMonitor(
        runner=runner,
        work_dir=work_dir,
        allow_commits=allow_commits,
    )
    return await monitor.reconcile(repo_url=repo_url, branch=branch, dry_run=dry_run)


def _generated_relative_path(result: AlembicResolveResult, checkout_path: Path) -> str:
    if result.generated_path_relative is not None:
        return result.generated_path_relative
    if result.generated_path is None:
        raise RuntimeError("resolver result changed without a generated path")
    return str(result.generated_path.relative_to(checkout_path))


_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slugify_repo(repo_url: str) -> str:
    tail = repo_url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _SLUG_RE.sub("-", tail) or "repo"


def _slugify_branch(branch: str) -> str:
    return _SLUG_RE.sub("-", branch).strip("-") or "branch"


def _summary_stale_reason(
    findings: Sequence[StalenessFinding],
    *,
    fallback: str | None,
) -> str | None:
    first = next(iter(findings), None)
    reason_code = getattr(first, "reason_code", None)
    return reason_code if isinstance(reason_code, str) else fallback
