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

from awf.control.executor import quality_methods as executor_quality_methods
from awf.db.enums import WorkspaceStatus


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
