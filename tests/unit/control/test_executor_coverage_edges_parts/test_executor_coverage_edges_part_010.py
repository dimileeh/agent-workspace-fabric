"""Focused behavioral tests for narrow ``quality_methods`` helper branches.

These drive module-level helpers in ``awf.control.executor.quality_methods``
directly with a lightweight ``SimpleNamespace`` fake executor (same pattern as
``test_executor_coverage_edges_part_009``) to exercise reachable branches the
heavier full-pipeline executor suites do not reach:

* the protected-quality-gate committed-output gate's empty-net-diff short
  circuit (no violation when ``base..HEAD`` touches no paths),
* the parallel-worker CPU limit's missing-reservation fall through, and
* the provider-recovery preparation's invalid-agent-runtime defaults guard.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult
from awf.control.executor import quality_methods as executor_quality_methods
from awf.control.executor.quality_gates import _PostAgentCommitStepError
from awf.control.quality_gates import QUALITY_GATE_POLICY_CHANGED_REASON_CODE
from awf.db.enums import WorkspaceStatus


@pytest.mark.unit
async def test_protected_quality_gate_committed_output_forwards_owner_id_on_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed-output protected violation owner-gates the block CAS.

    Without ``execution_owner_id`` the ``blocked`` transition is not
    owner-gated, so a stale executor that lost its claim to a newer claimant
    could still drive the transition and clobber the newer claimant's state —
    exactly the race the epoch-guarded CAS pattern prevents.
    """
    monkeypatch.setattr(
        executor_quality_methods,
        "committed_changed_paths_since",
        AsyncMock(return_value=["pyproject.toml"]),
    )
    monkeypatch.setattr(
        executor_quality_methods,
        "protected_file_diffs_for_committed_paths",
        AsyncMock(return_value={"pyproject.toml": "+fail_under = 70"}),
    )
    monkeypatch.setattr(
        executor_quality_methods,
        "find_protected_quality_gate_changes",
        lambda **_kwargs: ["policy-change"],
    )
    executor = SimpleNamespace(
        _runner=object(),
        _active_operator_grant_specs=AsyncMock(return_value=[]),
        enter_blocked_for_protected_violation=AsyncMock(return_value=True),
    )

    result = await executor_quality_methods._fail_if_protected_quality_gate_committed_output(
        executor,
        workspace_id="ws_block",
        worktree_path=Path("/tmp/worktree"),
        base_commit="0" * 40,
        owned_paths=["src/"],
        expected_status=WorkspaceStatus.validating,
        execution_owner_id="owner-committed-output",
    )

    assert result is True
    block_kwargs = executor.enter_blocked_for_protected_violation.await_args.kwargs
    assert block_kwargs["execution_owner_id"] == "owner-committed-output"


@pytest.mark.unit
async def test_mark_post_agent_commit_failed_forwards_owner_id_on_block() -> None:
    """A semantic-repair protected violation owner-gates the block CAS so a
    stale executor cannot clobber a newer claimant's state."""
    executor = SimpleNamespace(
        enter_blocked_for_protected_violation=AsyncMock(return_value=True),
    )
    error = _PostAgentCommitStepError(
        stage="post-agent pre-commit repair",
        result=CommandResult(returncode=1, stdout="", stderr=""),
        classification=None,
        reason_code_override=QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
        protected_violations=("policy-change",),
    )

    await executor_quality_methods._mark_post_agent_commit_failed(
        executor,
        workspace_id="ws_block",
        error=error,
        agent_run_reason_code=None,
        agent_run_details=None,
        agent_exit_note=None,
        upstream_failure_reason=None,
        execution_owner_id="owner-semantic-repair",
    )

    block_kwargs = executor.enter_blocked_for_protected_violation.await_args.kwargs
    assert block_kwargs["execution_owner_id"] == "owner-semantic-repair"


@pytest.mark.unit
async def test_protected_quality_gate_committed_output_passes_on_empty_net_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty net committed diff is not a protected-gate violation."""
    monkeypatch.setattr(
        executor_quality_methods,
        "committed_changed_paths_since",
        AsyncMock(return_value=[]),
    )
    executor = SimpleNamespace(
        _runner=object(),
        _mark_failed=AsyncMock(),
    )

    result = await executor_quality_methods._fail_if_protected_quality_gate_committed_output(
        executor,
        workspace_id="ws_empty",
        worktree_path=Path("/tmp/worktree"),
        base_commit="0" * 40,
        owned_paths=["src/"],
        expected_status=WorkspaceStatus.validating,
    )

    assert result is False
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_parallel_worker_cpu_limit_returns_none_without_active_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured worker count yields no limit when no reservation is active."""

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _ReservationRepo:
        def __init__(self, _session: object) -> None:
            return None

        async def active_for_workspace(self, _workspace_id: str) -> object | None:
            return None

    monkeypatch.setattr(
        executor_quality_methods,
        "ResourceReservationRepository",
        _ReservationRepo,
    )
    executor = SimpleNamespace(_session_factory=lambda: _Session())
    profile = SimpleNamespace(
        validation=SimpleNamespace(coverage=SimpleNamespace(parallel_workers=4)),
    )

    limit = await executor_quality_methods._parallel_worker_cpu_limit_for_workspace(
        executor,
        "ws_no_reservation",
        profile=profile,
    )

    assert limit is None


@pytest.mark.unit
async def test_prepare_provider_recovery_tolerates_invalid_agent_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable persisted agent runtime falls back to no default model."""

    class _Session:
        def __init__(self) -> None:
            self.commits = 0

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            return None

        async def get(self, _workspace_id: str) -> object:
            return SimpleNamespace(agent="nonexistent-runtime")

    captured: dict[str, object] = {}

    async def _fake_create_row(session: object, workspace_id: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return "terminal"

    monkeypatch.setattr(executor_quality_methods, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(
        executor_quality_methods,
        "create_provider_recovery_attempt_row",
        _fake_create_row,
    )

    session = _Session()
    executor = SimpleNamespace(
        _session_factory=lambda: session,
        _defaults_for=lambda _runtime: pytest.fail("defaults lookup must be skipped"),
    )

    await executor_quality_methods._prepare_provider_recovery(executor, "ws_bad_agent")

    # Invalid runtime → defaults is None → no effective default model is passed.
    assert captured["effective_default_model"] is None
    assert session.commits == 1
