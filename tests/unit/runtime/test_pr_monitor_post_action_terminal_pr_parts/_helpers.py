"""Shared builders/stubs for the #910 post-action terminal-PR suites.

Extracted verbatim from the original
``test_pr_monitor_post_action_terminal_pr.py`` so the suite could be split under
the 1,500-line maintainability cap. Keeping the ``PRStatus`` builder, the
scripted forge double, and the event readers in one private module — rather than
duplicating them across the part files — preserves a single source of truth for
the shapes every part asserts over.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import (
    WorkspaceEventRepository,
)
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _ProtectedScopePushBlock,
)

MOOT_EVENT = "workspace.monitor_action_moot"
MOOT_RECHECK_FAILED_EVENT = "workspace.monitor_action_moot_recheck_failed"


MOOT_EVENT = "workspace.monitor_action_moot"
MOOT_RECHECK_FAILED_EVENT = "workspace.monitor_action_moot_recheck_failed"


def _status(
    *,
    head_sha: str = "abc1234567890def",
    merged: bool = False,
    closed: bool = False,
    merge_commit_sha: str | None = None,
    threads: tuple[ReviewThread, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        base_behind_count=0,
        base_ref="development",
        merge_state_status=MergeStateStatus.CLEAN,
        merged=merged,
        closed=closed or merged,
        merge_commit_sha=merge_commit_sha,
    )


class _ScriptedGh:
    """Forge double returning scripted ``PRStatus`` snapshots in FIFO order."""

    def __init__(self, *statuses: PRStatus | Exception) -> None:
        """Store the scripted snapshots and start empty comment/fetch logs."""
        self._statuses: list[PRStatus | Exception] = list(statuses)
        self.posts: list[dict[str, object]] = []
        self.fetches: list[dict[str, object]] = []
        self.resolves: list[str] = []

    async def fetch_pr_status(
        self,
        *,
        repo: object,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        """Pop the next scripted snapshot, raising scripted forge faults."""
        del repo, base_behind_count
        self.fetches.append({"pr_number": pr_number, "retry": retry})
        if not self._statuses:
            raise AssertionError(
                "fetch_pr_status called with an exhausted script; add a snapshot "
                "to the test rather than masking an unexpected extra round-trip"
            )
        nxt = self._statuses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        """Record a PR comment that the guard is expected to suppress."""
        del repo
        self.posts.append({"pr_number": pr_number, "body": body})

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Record a thread resolution the guard is expected to suppress."""
        self.resolves.append(thread_id)

    async def aclose(self) -> None:
        """Match the single-use forge-client lifecycle the runner closes."""


def _respond_to_git_probes(cmd: FakeCommandRunner, *, head_sha: str = "localhead1234") -> None:
    """Answer the order-independent git probes these seams issue."""
    cmd.respond_when(lambda args: "rev-parse" in args, stdout=f"{head_sha}\n")
    cmd.respond_when(lambda args: "rev-list" in args, stdout="0\n")
    cmd.respond_when(lambda args: "status" in args, stdout="")
    cmd.respond_when(lambda args: "cat-file" in args, stdout="commit\n")


def _protected_block() -> _ProtectedScopePushBlock:
    return _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )


async def _moot_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[object]:
    async with factory() as session:
        return list(
            await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type=MOOT_EVENT,
                limit=10,
            )
        )


async def _recheck_failed_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[object]:
    async with factory() as session:
        return list(
            await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type=MOOT_RECHECK_FAILED_EVENT,
                limit=10,
            )
        )
