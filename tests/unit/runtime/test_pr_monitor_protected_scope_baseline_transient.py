"""#1221: a transient protected-scope baseline fetch must retry, then park (not fail).

The protected-scope committed-diff baseline is computed by fetching
``refs/heads/<remote_branch>``. A transient network/DNS/TLS failure on that fetch
used to map straight to the TERMINAL ``PROTECTED_SCOPE_DIFF_UNAVAILABLE`` reason,
so the workspace failed and terminal cleanup discarded the unpushed repair commit.
These tests pin the bounded retry (reusing the base-fetch classifier and backoff),
the blocked park that preserves the worktree and the commit when retries run out,
and that a genuine protected-scope violation and a deterministic fetch failure
behave exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
)
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import OperatorHint
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReportCiFailure,
)
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass as fix_pass_module
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.pre_push_validation import _PrePushValidationResult
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
    mark_provider_park_stranded_edits,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeBaselineUnavailableError,
    ProtectedScopeDiffError,
)
from awf.runtime.validation_worktree_types import ValidationWorktreeCheck
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _mark_git_worktree,
    _validation_result,
)

_DNS_STDERR = (
    "fatal: unable to access 'https://github.com/dimileeh/aira-web/': "
    "Could not resolve host: github.com"
)
_MISSING_REF_STDERR = "fatal: couldn't find remote ref refs/heads/x"
_REPAIR_COMMIT_SHA = "f" * 40
_PROTECTED_WORKFLOW_OLD = "name: CI\njobs:\n  t:\n    steps:\n      - run: pytest"
_PROTECTED_WORKFLOW_NEW = f"{_PROTECTED_WORKFLOW_OLD}\n        continue-on-error: true"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


def _status() -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        blocking_reviews=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    cmd: FakeCommandRunner,
    *,
    sleep: RecordedSleep | None = None,
    adapter: FakeAdapter | None = None,
    max_retries: int = 2,
) -> PullRequestMonitorRunner:
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter or FakeAdapter(),
        sleep_fn=sleep or RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._runner_config = replace(
        runner._runner_config,
        transient_base_fetch_max_retries=max_retries,
        transient_base_fetch_initial_backoff_seconds=5.0,
        transient_base_fetch_max_backoff_seconds=120.0,
    )
    return runner


def _queue_ci_repair_until_baseline_fetch(cmd: FakeCommandRunner) -> None:
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")  # clean worktree: agent committed locally itself
    # #910: post-action PR re-check before the protected-scope evaluation.
    cmd.queue_result(returncode=0, stdout=pr_payload())


def _queue_sync_base_until_baseline_fetch(cmd: FakeCommandRunner) -> None:
    cmd.queue_result(returncode=0, stdout="abc123\n")
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch base
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # merged base (#1167)
    cmd.queue_result(returncode=0, stdout=pr_payload())  # #910 re-check before the push


async def _run_ci_fix(runner: PullRequestMonitorRunner, workspace_id: str, tmp_path: Path) -> Any:
    return await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        status=_status(),
    )


async def _run_sync_base(
    runner: PullRequestMonitorRunner, workspace_id: str, tmp_path: Path
) -> Any:
    return await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
    )


async def _events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, event_type: str
) -> list[Any]:
    async with factory() as s:
        return list(
            await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id, event_type=event_type, limit=20
            )
        )


async def _workspace(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> Workspace:
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
    assert workspace is not None
    return workspace


def _call_args(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls]


def _assert_no_destructive_git(cmd: FakeCommandRunner) -> None:
    for args in _call_args(cmd):
        assert not (args[:1] == ["git"] and "push" in args), args
        assert not (args[:1] == ["git"] and "reset" in args), args
        assert not (args[:1] == ["git"] and "worktree" in args and "remove" in args), args
        assert not (args[:1] == ["docker"] and "down" in args), args


def _violation() -> QualityGateViolation:
    return QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/workflows/**",
    )


# --- retry at the fetch choke point -------------------------------------------------


@pytest.mark.unit
async def test_baseline_fetch_retries_transient_dns_failure_then_succeeds(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktree"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr=_DNS_STDERR)
    cmd.queue_result(returncode=0)  # retry fetch succeeds
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z("src/fix.py"))
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep)

    local_base, changed = await runner._remote_branch_diff_base_and_changed_paths(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch="awf/ws_branch",
    )

    assert (local_base, changed) == ("merge-base-sha", ("src/fix.py",))
    assert sleep.calls == [5.0]
    fetch = _git_worktree_command(worktree, "fetch", "origin", "refs/heads/awf/ws_branch")
    assert _call_args(cmd).count(fetch) == 2
    retrying = await _events(
        factory, workspace_id, "monitor.protected_scope_baseline_fetch_transient_retrying"
    )
    assert len(retrying) == 1
    assert retrying[0].reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY"
    payload = retrying[0].payload
    assert payload["context"] == "protected_scope_diff_baseline"
    assert payload["retry_number"] == 1
    assert payload["max_retries"] == 2
    assert payload["wait_seconds"] == 5.0
    assert payload["remote_branch"] == "awf/ws_branch"
    assert "Could not resolve host" in payload["message"]
    assert not await _events(
        factory,
        workspace_id,
        "monitor.protected_scope_baseline_fetch_transient_retry_exhausted",
    )


@pytest.mark.unit
async def test_baseline_fetch_raises_baseline_unavailable_after_bounded_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    for _ in range(3):
        cmd.queue_result(returncode=128, stderr=_DNS_STDERR)
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep)

    with pytest.raises(ProtectedScopeBaselineUnavailableError) as exc_info:
        await runner._remote_branch_diff_base_and_changed_paths(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_branch",
        )

    # Same fail-closed message as before; only the type is narrower.
    assert isinstance(exc_info.value, ProtectedScopeDiffError)
    assert "fetch refs/heads/awf/ws_branch exit=128" in str(exc_info.value)
    assert sleep.calls == [5.0, 10.0]
    assert len(cmd.calls) == 3
    exhausted = await _events(
        factory,
        workspace_id,
        "monitor.protected_scope_baseline_fetch_transient_retry_exhausted",
    )
    assert len(exhausted) == 1
    assert exhausted[0].reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED"
    assert exhausted[0].payload["retry_number"] == 2


@pytest.mark.unit
async def test_baseline_fetch_non_transient_failure_fails_closed_without_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr=_MISSING_REF_STDERR)
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep)

    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._remote_branch_diff_base_and_changed_paths(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_branch",
        )

    assert not isinstance(exc_info.value, ProtectedScopeBaselineUnavailableError)
    assert sleep.calls == []
    assert len(cmd.calls) == 1
    assert not await _events(
        factory, workspace_id, "monitor.protected_scope_baseline_fetch_transient_retrying"
    )


@pytest.mark.unit
async def test_restore_verification_fetch_retries_then_raises_baseline_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    for _ in range(2):
        cmd.queue_result(returncode=128, stderr="error: GnuTLS recv error (-110)")
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep, max_retries=1)

    with pytest.raises(ProtectedScopeBaselineUnavailableError) as exc_info:
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            violations=[_violation()],
            remote_branch="awf/ws_branch",
        )

    assert str(exc_info.value).startswith(
        "Could not verify protected-scope restore against the remote PR branch"
    )
    assert sleep.calls == [5.0]
    assert len(cmd.calls) == 2
    retrying = await _events(
        factory, workspace_id, "monitor.protected_scope_baseline_fetch_transient_retrying"
    )
    assert retrying[0].payload["context"] == "protected_scope_restore_verification"


@pytest.mark.unit
async def test_restore_verification_fetch_non_transient_failure_is_unchanged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr=_MISSING_REF_STDERR)
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep)

    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            violations=[_violation()],
            remote_branch="awf/ws_branch",
        )

    assert not isinstance(exc_info.value, ProtectedScopeBaselineUnavailableError)
    assert sleep.calls == []


# --- acceptance: end-to-end through the CI-repair push seam ----------------------


@pytest.mark.unit
async def test_ci_repair_transient_baseline_fetch_then_success_pushes_normally(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Acceptance #1: a transient fetch failure followed by success validates and pushes."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _queue_ci_repair_until_baseline_fetch(cmd)
    cmd.queue_result(returncode=128, stderr=_DNS_STDERR)  # baseline fetch: DNS blip
    cmd.queue_result(returncode=0)  # baseline fetch retry: success
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout="")  # no protected changes vs the PR branch
    cmd.queue_result(returncode=0, stderr="To github.com:dimileeh/aira-web.git")  # git push
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep, adapter=adapter)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    push_result = await _run_ci_fix(runner, workspace_id, tmp_path)

    assert push_result.failed is False
    assert push_result.pushed is True
    assert sleep.calls == [5.0]
    assert any(args[:1] == ["git"] and "push" in args for args in _call_args(cmd))
    retrying = await _events(
        factory, workspace_id, "monitor.protected_scope_baseline_fetch_transient_retrying"
    )
    assert [event.reason_code for event in retrying] == ["GIT_BASE_FETCH_TRANSIENT_RETRY"]
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_ci_repair_persistent_baseline_outage_parks_blocked_and_preserves_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Acceptance #2: a persistent fetch failure parks the workspace instead of failing it."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _queue_ci_repair_until_baseline_fetch(cmd)
    for _ in range(3):  # initial fetch + 2 bounded retries
        cmd.queue_result(returncode=128, stderr=_DNS_STDERR)
    cmd.queue_result(returncode=0, stdout=pr_payload())  # #910 re-check before parking
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep, adapter=adapter)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    terminate_failed = AsyncMock()
    runner._terminate_failed = terminate_failed  # type: ignore[method-assign]
    state = MonitorState()

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    terminate_failed.assert_not_awaited()
    assert sleep.calls == [5.0, 10.0]
    _assert_no_destructive_git(cmd)
    assert worktree.is_dir()

    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.block_reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert workspace.block_resume_phase == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    assert workspace.block_violations == []
    # The "preserved ungranted protected commit" marker would make a directive
    # resume refuse to push this legitimate repair commit, so it must stay unset.
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in (
        workspace.monitor_threads_addressed or {}
    )
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids

    parked = await _events(
        factory, workspace_id, "workspace.monitor_protected_scope_diff_unavailable_parked"
    )
    assert len(parked) == 1
    assert parked[0].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert parked[0].payload["preserved_head_sha"] == _REPAIR_COMMIT_SHA
    assert parked[0].payload["resume_phase"] == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    assert parked[0].payload["pushed"] is False
    exhausted = await _events(
        factory,
        workspace_id,
        "monitor.protected_scope_baseline_fetch_transient_retry_exhausted",
    )
    assert len(exhausted) == 1
    blocked_events = await _events(
        factory, workspace_id, "workspace.monitor_protected_scope_push_blocked"
    )
    assert [event.reason_code for event in blocked_events] == ["PROTECTED_SCOPE_DIFF_UNAVAILABLE"]
    assert blocked_events[0].payload["transient"] is True

    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
    ci_operation = next(op for op in operations if op.type == "ci_repair")
    assert ci_operation.status == OperationStatus.succeeded.value
    assert ci_operation.result is not None
    assert ci_operation.result["outcome"] == "protected_scope_paused"
    assert ci_operation.result["reason_code"] == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"


@pytest.mark.unit
async def test_ci_repair_genuine_protected_violation_still_pauses_exactly_as_before(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Acceptance #3 (regression): a real violation keeps the WS-2 protected pause."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()
    cmd = FakeCommandRunner()
    _queue_ci_repair_until_baseline_fetch(cmd)
    cmd.queue_result(returncode=0)  # baseline fetch succeeds first time
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml"))
    cmd.queue_result(returncode=0)  # cat-file base:path
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_OLD)
    cmd.queue_result(returncode=0)  # cat-file HEAD:path
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_NEW)
    cmd.queue_result(returncode=0, stdout=pr_payload())  # #910 re-check before pausing
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep, adapter=adapter)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    push_result = await _run_ci_fix(runner, workspace_id, tmp_path)

    assert push_result.paused_into_blocked is True
    assert push_result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert sleep.calls == []
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.block_reason_code == "QUALITY_GATE_POLICY_CHANGED"
    assert workspace.block_violations
    assert (workspace.monitor_threads_addressed or {})[
        _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY
    ] == _REPAIR_COMMIT_SHA
    assert not await _events(
        factory, workspace_id, "workspace.monitor_protected_scope_diff_unavailable_parked"
    )
    assert not await _events(
        factory, workspace_id, "monitor.protected_scope_baseline_fetch_transient_retrying"
    )


@pytest.mark.unit
async def test_ci_repair_non_transient_baseline_failure_stays_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression: a deterministic fetch failure keeps today's terminal result."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _queue_ci_repair_until_baseline_fetch(cmd)
    cmd.queue_result(returncode=128, stderr=_MISSING_REF_STDERR)
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep, adapter=adapter)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    push_result = await _run_ci_fix(runner, workspace_id, tmp_path)

    assert push_result.paused_into_blocked is False
    assert push_result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert push_result.terminal_monitor_failure is True
    assert sleep.calls == []
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    blocked_events = await _events(
        factory, workspace_id, "workspace.monitor_protected_scope_push_blocked"
    )
    assert "transient" not in blocked_events[0].payload


# --- per-site wiring ---------------------------------------------------------------


@pytest.mark.unit
async def test_sync_base_persistent_baseline_outage_parks_with_sync_base_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _queue_sync_base_until_baseline_fetch(cmd)
    cmd.queue_result(returncode=128, stderr=_DNS_STDERR)  # baseline fetch, no retry budget
    cmd.queue_result(returncode=0, stdout=pr_payload())  # #910 re-check before parking
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    sleep = RecordedSleep()
    runner = _runner(factory, tmp_path, cmd, sleep=sleep, max_retries=0)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    push_result = await _run_sync_base(runner, workspace_id, tmp_path)

    assert push_result.paused_into_blocked is True
    assert push_result.terminal_monitor_failure is False
    assert push_result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert sleep.calls == []
    _assert_no_destructive_git(cmd)
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.block_resume_phase == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE


@pytest.mark.unit
async def test_sync_base_non_transient_baseline_failure_stays_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _queue_sync_base_until_baseline_fetch(cmd)
    cmd.queue_result(returncode=128, stderr=_MISSING_REF_STDERR)
    runner = _runner(factory, tmp_path, cmd)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    push_result = await _run_sync_base(runner, workspace_id, tmp_path)

    assert push_result.paused_into_blocked is False
    assert push_result.terminal_monitor_failure is True
    assert push_result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"


@pytest.mark.unit
async def test_commit_sink_baseline_unavailable_parks_instead_of_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    runner = _runner(factory, tmp_path, cmd)

    parked = await runner._protected_scope_diff_unavailable_push_result(
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        exc=ProtectedScopeBaselineUnavailableError("fetch failed: Could not resolve host"),
    )

    assert parked.paused_into_blocked is True
    assert parked.terminal_monitor_failure is False
    assert parked.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert parked.details is not None
    assert parked.details["phase"] == "pre_push_committed_diff"
    assert parked.details["transient_baseline_unavailable"] is True
    assert parked.details["preserved_head_sha"] == _REPAIR_COMMIT_SHA
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value

    plain = await runner._protected_scope_diff_unavailable_push_result(
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        exc=ProtectedScopeDiffError("merge-base failed"),
    )
    assert plain.paused_into_blocked is False
    assert plain.terminal_monitor_failure is True


@pytest.mark.unit
async def test_park_cas_lost_returns_plain_failure_without_touching_row(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        await s.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(status=WorkspaceStatus.cancelled.value)
        )
        await s.commit()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")
    runner = _runner(factory, tmp_path, cmd)

    result = await runner._park_for_protected_scope_baseline_unavailable(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        remote_branch=f"awf/{workspace_id}",
        resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
        message="baseline unavailable",
    )

    assert result.paused_into_blocked is False
    assert result.failed is True
    assert result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert result.stderr == "baseline unavailable"
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.cancelled.value
    assert workspace.block_reason_code is None
    assert not await _events(
        factory, workspace_id, "workspace.monitor_protected_scope_diff_unavailable_parked"
    )


@pytest.mark.unit
async def test_park_returns_moot_result_when_pr_merged_during_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True, closed=True))
    runner = _runner(factory, tmp_path, cmd)

    result = await runner._park_for_protected_scope_baseline_unavailable(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        remote_branch=f"awf/{workspace_id}",
        resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
        message="baseline unavailable",
        pr_number=42,
    )

    assert result.pr_terminal is not None
    assert result.paused_into_blocked is False
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_validated_push_parks_when_validation_carries_transient_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    runner = _runner(factory, tmp_path, cmd)
    runner._deps.validation = object()  # type: ignore[assignment]

    async def _validation(*_args: object, **_kwargs: object) -> _PrePushValidationResult:
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=None,
            workspace_head_sha=_REPAIR_COMMIT_SHA,
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            message="protected-scope diff unavailable",
            extra_details={"transient_baseline_unavailable": True},
        )

    monkeypatch.setattr(
        pre_push_validation_module, "_run_pre_push_validation_with_fix_passes", _validation
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert result.details is not None
    # Not ``pre_push_validation``: sync-base would otherwise swap the park for its
    # infrastructure-budget park while the row is already ``blocked``.
    assert result.details["phase"] == "pre_push_committed_diff"
    assert result.details["pre_push_validation"]["error_message"] == (
        "protected-scope diff unavailable"
    )
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.block_resume_phase == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE


@pytest.mark.unit
async def test_validated_push_park_keeps_caller_sync_base_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync-base validation park must not fall back to the push phase.

    Otherwise the guide resume selects the ordinary protected-scope check, which
    re-blocks on protected changes the base merge legitimately pulled in.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    runner = _runner(factory, tmp_path, cmd)
    runner._deps.validation = object()  # type: ignore[assignment]

    async def _validation(*_args: object, **_kwargs: object) -> _PrePushValidationResult:
        return _PrePushValidationResult(
            passed=False,
            validation_run_id=None,
            workspace_head_sha=_REPAIR_COMMIT_SHA,
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            message="protected-scope diff unavailable",
            extra_details={"transient_baseline_unavailable": True},
        )

    monkeypatch.setattr(
        pre_push_validation_module, "_run_pre_push_validation_with_fix_passes", _validation
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        protected_scope_resume_phase=MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
    )

    assert result.paused_into_blocked is True
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.block_resume_phase == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE


@pytest.mark.unit
async def test_sync_base_validated_push_requests_sync_base_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    _queue_sync_base_until_baseline_fetch(cmd)
    runner = _runner(factory, tmp_path, cmd)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    captured: dict[str, object] = {}

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _pushed(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)

    await _run_sync_base(runner, workspace_id, tmp_path)

    assert captured["protected_scope_resume_phase"] == (
        MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    )


@pytest.mark.unit
async def test_fix_pass_baseline_unavailable_marks_validation_result_transient(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    failing = _PrePushValidationResult(
        passed=False,
        validation_run_id="vr_1",
        workspace_head_sha=_REPAIR_COMMIT_SHA,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="tests failed",
        extra_details={"existing": "kept"},
    )
    monkeypatch.setattr(
        pre_push_validation_module, "_run_pre_push_validation", AsyncMock(return_value=failing)
    )
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_commands",
        AsyncMock(return_value=("pytest",)),
    )
    # Pretend a command failed so the loop reaches the fix pass.
    monkeypatch.setattr(_PrePushValidationResult, "first_failure", property(lambda _: object()))

    for exc, transient in (
        (ProtectedScopeBaselineUnavailableError("dns"), True),
        (ProtectedScopeDiffError("merge-base"), False),
    ):
        monkeypatch.setattr(
            pre_push_validation_module,
            "_run_pre_push_validation_fix_pass",
            AsyncMock(side_effect=exc),
        )
        result = await pre_push_validation_module._run_pre_push_validation_with_fix_passes(
            runner,
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch=f"awf/{workspace_id}",
            remote_url=None,
            state=None,
        )
        assert result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
        assert result.extra_details is not None
        assert result.extra_details["existing"] == "kept"
        assert result.extra_details.get("transient_baseline_unavailable", False) is transient


@pytest.mark.unit
@pytest.mark.parametrize("transient", [False, True])
async def test_fix_pass_sink_baseline_unavailable_marks_stranded_edits_only_when_transient(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient: bool,
) -> None:
    """The park keeps the fix pass's uncommitted edits, so the resume must commit them.

    The commit sink fails before ``git commit``; without the stranded-edits marker the
    guide resume's repair-start guard rejects that dirt as ``PRE_EXISTING_DIRTY_WORKTREE``
    (PRRT_kwDOSJAM6s621aJ-). A non-transient failure stays terminal and unmarked.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    _mark_git_worktree(tmp_path / "worktrees" / workspace_id)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    error_type = ProtectedScopeBaselineUnavailableError if transient else ProtectedScopeDiffError
    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(runner, "_rev_parse_head", AsyncMock(return_value=_REPAIR_COMMIT_SHA))
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=error_type("dns")))
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(fix_pass_module, "verify_head_object_exists", AsyncMock(return_value=True))
    state = MonitorState()

    with pytest.raises(error_type):
        await pre_push_validation_module._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch=f"awf/{workspace_id}",
            remote_url=None,
            state=state,
            validation_result=_PrePushValidationResult(
                passed=False,
                validation_run_id="vr_failed",
                workspace_head_sha=_REPAIR_COMMIT_SHA,
                reason_code="PRE_PUSH_VALIDATION_FAILED",
                message="PR monitor pre-push validation failed: COMMAND_FAILED",
                validation_reason_code="COMMAND_FAILED",
                result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
            ),
            pass_number=1,
            total_passes=3,
            validation_commands=("pytest -q",),
        )

    if transient:
        assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == (
            _REPAIR_COMMIT_SHA
        )
    else:
        assert PROVIDER_PARK_STRANDED_EDITS_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_validated_push_park_persists_stranded_edits_evidence_before_blocking(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marked park records what it left dirty and persists it with the marker.

    The guide resume reloads the row and commits exactly the recorded edits before
    its pre-existing-dirty guard runs (PRRT_kwDOSJAM6s621aJ-).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktree_path = tmp_path / "worktrees" / workspace_id
    (worktree_path / "src").mkdir(parents=True)
    (worktree_path / "src" / "fix.py").write_text("fixed\n")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")  # preserved HEAD
    runner = _runner(factory, tmp_path, cmd)
    runner._deps.validation = object()  # type: ignore[assignment]
    state = MonitorState()
    mark_provider_park_stranded_edits(state, _REPAIR_COMMIT_SHA)
    monkeypatch.setattr(
        pre_push_validation_module,
        "_run_pre_push_validation_with_fix_passes",
        AsyncMock(
            return_value=_PrePushValidationResult(
                passed=False,
                validation_run_id=None,
                workspace_head_sha=_REPAIR_COMMIT_SHA,
                reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
                message="protected-scope diff unavailable",
                extra_details={"transient_baseline_unavailable": True},
            )
        ),
    )
    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        AsyncMock(
            return_value=_GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                reason_code="PRE_EXISTING_DIRTY_WORKTREE",
                details={"paths": ["src/fix.py"]},
            )
        ),
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
    )

    assert result.paused_into_blocked is True
    workspace = await _workspace(factory, workspace_id)
    assert workspace.status == WorkspaceStatus.blocked.value
    persisted = workspace.monitor_threads_addressed or {}
    assert persisted[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _REPAIR_COMMIT_SHA
    evidence = json.loads(persisted[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY])
    assert list(evidence) == ["src/fix.py"]
    assert evidence["src/fix.py"].startswith("file:")


@pytest.mark.unit
def test_worktree_check_extra_details_merge_into_details() -> None:
    check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        extra_details={"transient_baseline_unavailable": True},
    )

    details = check.details()

    assert details["transient_baseline_unavailable"] is True
    assert details["reason_code"] == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "transient_baseline_unavailable" not in ValidationWorktreeCheck(clean=True).details()


@pytest.mark.unit
async def test_operator_hint_resume_park_keeps_original_phase_and_clears_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _sync_base_phase(_workspace_id: str) -> str:
        return MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE

    async def _transient_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="baseline unavailable",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            transient_baseline_unavailable=True,
        )

    async def _parked(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="parked",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            paused_into_blocked=True,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_resolve_block_resume_phase", _sync_base_phase)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _transient_block)
    monkeypatch.setattr(runner, "_repair_protected_scope_commits_before_push", _parked)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_park",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_park",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        base_branch="development",
    )

    assert result.paused_into_blocked is True
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    # The park bumps the block epoch, superseding this hint (like a re-block).
    assert state.pending_operator_hint is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "block_phase",
    [MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE, MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE],
)
async def test_operator_hint_validated_push_requests_original_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    block_phase: str,
) -> None:
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_phase",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _none(*_args: object, **_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _block_phase(_workspace_id: str) -> str:
        return block_phase

    async def _pushed(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(pushed=False, failed=True, returncode=1, stderr="stop")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_resolve_block_resume_phase", _block_phase)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)

    await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_phase",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_phase",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        base_branch="development",
    )

    assert captured["protected_scope_resume_phase"] == block_phase


@pytest.mark.unit
async def test_operator_hint_commit_sink_park_clears_hint_instead_of_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_sink",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _raise_baseline(**_kwargs: object) -> None:
        raise ProtectedScopeBaselineUnavailableError("dns")

    async def _parked(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="parked",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            paused_into_blocked=True,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise_baseline)
    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_push_result", _parked)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_sink_park",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_sink_park",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert state.pending_operator_hint is None


async def _mark_baseline_parked(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, *, resume_phase: str | None
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                block_reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
                block_resume_phase=resume_phase,
            )
        )
        await s.commit()


@pytest.mark.unit
async def test_baseline_park_resume_pending_tracks_unsettled_park_only(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path, FakeCommandRunner())

    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is False
    await _mark_baseline_parked(
        factory, workspace_id, resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    )
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True
    # A finalized resume clears the phase: the kept commit was published or dropped.
    await _mark_baseline_parked(factory, workspace_id, resume_phase=None)
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is False
    assert await runner._protected_scope_baseline_park_resume_pending("ws_missing") is False


@pytest.mark.unit
async def test_operator_hint_resume_of_baseline_park_keeps_commit_on_rejected_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grantless guide resuming a baseline park must not reset --hard its commit.

    The PR branch advanced while the workspace was blocked, so the push is rejected
    non-fast-forward. The default resync would fetch the remote and reset the kept
    repair away; the resume suppresses it and parks the hint for a human instead.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_baseline_parked(
        factory, workspace_id, resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    )
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_rejected",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _rejected(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="! [rejected] HEAD -> awf/x (non-fast-forward)",
            reason_code="GIT_PUSH_REJECTED_NON_FAST_FORWARD",
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_validated_git_push_result", _rejected)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert captured["allow_resync_on_rejection"] is False
    assert result.failed is False
    assert result.returncode == 1
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "kept" in (state.pending_operator_hint.status_reason or "")
    # The park stays unsettled, so a follow-up guide keeps suppressing the reset.
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True
