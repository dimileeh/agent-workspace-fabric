"""Feature-monitor merge path of the review-thread disposition gate (#1018).

A feature monitor audits review threads before it takes the merge lock and again
after the in-lock status refresh, so a thread resolved silently (or feedback
opened) while it waited for the lock still blocks the merge. Only the pre-lock
audit retries in transport; the in-lock re-audit fails fast and never backs off
holding the merge lock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import ReviewThreadAudit
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _disposition_unverified_key
from awf.runtime.pr_monitor import Merge, MonitorState
from tests.postgres import postgres_test_engine
from tests.unit.runtime._merge_methods_fixtures import (
    _TEST_DEFAULT_BASE_BRANCH,
    _TEST_PR_NUMBER,
    _TEST_REPO,
    _execute_merge,
    _mergeable_status,
    _MergeMethodClient,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import settled_baseline_state
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _FRESH,
    _SILENT,
    _VIEWER,
    _attention,
    _audit_events,
    _marker,
    _thread,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _AuditMergeClient(_MergeMethodClient):
    """The merge-method double, serving a canned review-thread audit."""

    def __init__(self, audit: ReviewThreadAudit) -> None:
        super().__init__(merge_results=["MERGESHA123"])
        self.audit = audit

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True, thread_id: str | None = None
    ) -> ReviewThreadAudit:
        assert thread_id is None
        del repo, pr_number, retry
        return self.audit


async def test_feature_monitor_merge_refused_by_gate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(_thread(_SILENT),))
    )

    terminal, state, _, workspace_id = await _execute_merge(
        factory=factory, tmp_path=tmp_path, gh=gh, state=settled_baseline_state()
    )

    assert terminal is False
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_DISPOSITION_UNVERIFIED" in gh.comments[0]
    assert _SILENT in gh.comments[0]
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value


async def test_feature_monitor_merge_proceeds_with_verified_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _AuditMergeClient(
        ReviewThreadAudit(
            supported=True,
            viewer_login=_VIEWER,
            threads=(_thread("PRRT_test_ok", bodies=(_marker("fixed", "PRRT_test_ok"),)),),
        )
    )

    terminal, _, _, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is True
    assert gh.merge_calls == ["squash"]


async def test_feature_merge_waits_on_transient_audit_fault(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _TransientAuditMergeClient(_AuditMergeClient):
        async def fetch_review_thread_audit(
            self, *, repo: RepoRef, pr_number: int, retry: bool = True, thread_id: str | None = None
        ) -> ReviewThreadAudit:
            assert thread_id is None
            del repo, pr_number, retry
            raise GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502")

    gh = _TransientAuditMergeClient(ReviewThreadAudit(supported=True))

    terminal, state, sleep_fn, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is False
    assert gh.merge_calls == []
    assert gh.comments == []
    assert sleep_fn.calls  # backed off before the next poll
    assert (
        state.threads_addressed_ids["__awf_forge_transient_retry_count:review_thread_audit"] == "1"
    )


class _SequencedAuditMergeClient(_AuditMergeClient):
    """Serves one audit (or forge fault) per call, noting the in-lock status refreshes so far."""

    def __init__(self, *results: ReviewThreadAudit | ForgeClientError) -> None:
        super().__init__(ReviewThreadAudit(supported=True, viewer_login=_VIEWER))
        self.results = list(results)
        self.status_refreshes_at_audit: list[int] = []
        self.audit_retries: list[bool] = []

    async def fetch_review_thread_audit(
        self, *, repo: RepoRef, pr_number: int, retry: bool = True, thread_id: str | None = None
    ) -> ReviewThreadAudit:
        assert thread_id is None
        del repo, pr_number
        self.status_refreshes_at_audit.append(self.fetch_pr_status_calls)
        self.audit_retries.append(retry)
        result = self.results.pop(0)
        if isinstance(result, ForgeClientError):
            raise result
        return result


class _LockLog:
    """Merge coordinator + sleep double that logs whether each sleep holds the merge lock."""

    def __init__(self) -> None:
        self.held = False
        self.sleeps_under_lock: list[bool] = []

    @asynccontextmanager
    async def serialized_merge(self, *, repo_url: str, base_branch: str) -> AsyncIterator[None]:
        del repo_url, base_branch
        self.held = True
        try:
            yield
        finally:
            self.held = False

    async def sleep(self, seconds: float) -> None:
        del seconds
        self.sleeps_under_lock.append(self.held)


async def _merge_under_lock_log(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    gh: _MergeMethodClient,
    *,
    state: MonitorState | None = None,
    lock: _LockLog | None = None,
) -> tuple[bool | None, MonitorState, _LockLog, str]:
    workspace_id = await seed_monitoring_workspace(factory)
    gh.expect_context(
        repo=_TEST_REPO, pr_number=_TEST_PR_NUMBER, base_branch=_TEST_DEFAULT_BASE_BRANCH
    )
    lock = lock or _LockLog()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=lock.sleep,  # type: ignore[arg-type]
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
        merge_coordinator=lock,
    )
    state = state if state is not None else settled_baseline_state()
    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        repo=_TEST_REPO,
        pr_number=_TEST_PR_NUMBER,
        status=_mergeable_status(),
        state=state,
        base_branch=_TEST_DEFAULT_BASE_BRANCH,
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    return terminal, state, lock, workspace_id


async def test_feature_merge_reaudits_after_in_lock_status_refresh(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6vMb1w: a thread AWF's identity resolves silently while the
    monitor waits for the merge lock / pre-merge settle drops out of the in-lock
    status refresh, so ``decide`` still says Merge; the in-lock re-audit refuses."""
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER),
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER, threads=(_thread(_SILENT),)),
    )

    terminal, state, _, workspace_id = await _execute_merge(
        factory=factory, tmp_path=tmp_path, gh=gh
    )

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1]  # re-audit follows the in-lock refresh
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_DISPOSITION_UNVERIFIED" in gh.comments[0]
    assert _SILENT in gh.comments[0]
    assert state.threads_addressed_ids[_disposition_unverified_key(_SILENT)]
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.parametrize("fresh_audit", [0, 1])
async def test_feature_merge_waits_for_a_poll_that_lists_feedback_opened_after_its_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    fresh_audit: int,
) -> None:
    """PRRT_kwDOSJAM6s6vVxR0: neither the pre-lock audit nor the in-lock re-audit merges
    over a thread opened after the status ``decide`` merged on."""
    clean = ReviewThreadAudit(supported=True, viewer_login=_VIEWER)
    fresh = ReviewThreadAudit(
        supported=True,
        viewer_login=_VIEWER,
        threads=(_thread(_FRESH, resolved=False, external=("P1",)),),
    )
    gh = _SequencedAuditMergeClient(*[clean] * fresh_audit, fresh)

    terminal, _, lock, _ = await _merge_under_lock_log(factory, tmp_path, gh)

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1][: fresh_audit + 1]
    assert gh.merge_calls == []
    assert gh.comments == []
    assert lock.sleeps_under_lock == [False]


async def test_feature_merge_in_lock_reaudit_fails_fast(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6vbU2K: only the pre-lock audit retries in transport; the in-lock
    re-audit fails fast, so a transient blip never backs off holding the merge lock."""
    clean = ReviewThreadAudit(supported=True, viewer_login=_VIEWER)
    gh = _SequencedAuditMergeClient(clean, clean)

    terminal, _, _, _ = await _execute_merge(factory=factory, tmp_path=tmp_path, gh=gh)

    assert terminal is True
    assert gh.status_refreshes_at_audit == [0, 1]
    assert gh.audit_retries == [True, False]


async def test_feature_merge_in_lock_transient_audit_fault_backs_off_after_release(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The in-lock re-audit never sleeps holding the merge lock: it backs off after release."""
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER),
        GitHubClientError(operation="gh api graphql", returncode=1, stderr="HTTP 502"),
    )

    terminal, state, lock, _ = await _merge_under_lock_log(factory, tmp_path, gh)

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1]
    assert gh.merge_calls == []
    assert gh.comments == []
    assert lock.sleeps_under_lock == [False]
    assert (
        state.threads_addressed_ids["__awf_forge_transient_retry_count:review_thread_audit"] == "1"
    )


async def test_feature_merge_in_lock_permanent_audit_fault_notifies_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _SequencedAuditMergeClient(
        ReviewThreadAudit(supported=True, viewer_login=_VIEWER),
        GitHubClientError(
            operation="gh api graphql", returncode=1, stderr="GraphQL: Could not resolve to a node"
        ),
    )

    terminal, _, lock, workspace_id = await _merge_under_lock_log(factory, tmp_path, gh)

    assert terminal is False
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_AUDIT_UNAVAILABLE" in gh.comments[0]
    assert True not in lock.sleeps_under_lock
    events = await _audit_events(factory, workspace_id)
    assert [(e["outcome"], e["reason_code"]) for e in events] == [
        ("failed", "REVIEW_THREAD_AUDIT_UNAVAILABLE")
    ]
