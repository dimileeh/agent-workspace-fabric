"""Conformance-stall failure-builder coverage for executor planning ops.

Exercises ``WorkspaceExecutor._build_conformance_stall_failure`` baseline,
event-recording, and workspace-vanished branches. Split out of
``test_planning_ops_branch_edges.py`` to keep each test module within the
first-party line-length guardrail.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import planning_ops as planning_ops
from awf.runtime.planning import (
    AGENT_STALLED_IN_CONFORMANCE,
    ConformanceStallEvidence,
    ConformanceStallKind,
    PlanConformanceReport,
    PlanConformanceStatus,
)


def _executor_with_runner(runner: FakeCommandRunner, tmp_path: Path) -> WorkspaceExecutor:
    executor = WorkspaceExecutor(
        session_factory=object(),  # type: ignore[arg-type]
        runner=runner,
        compose=object(),  # type: ignore[arg-type]
        validation=object(),  # type: ignore[arg-type]
        pr_creator=object(),  # type: ignore[arg-type]
        config=ExecutorConfig(
            worktrees_root=tmp_path / "worktrees",
            compose_projects_root=tmp_path / "compose",
        ),
    )
    executor._update_subphase = AsyncMock()  # type: ignore[method-assign]
    return executor


def _stall_evidence() -> ConformanceStallEvidence:
    return ConformanceStallEvidence(
        kind=ConformanceStallKind.no_output,
        iteration_index=1,
        elapsed_seconds=700.0,
        no_output_seconds=700.0,
        repeated_output_count=0,
        last_report_digest=None,
        plan_path="docs/plan.md",
        report_path="docs/report.json",
    )


class _CommittingSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _CommittingSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.unit
async def test_build_conformance_stall_failure_without_baseline_skips_commit_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``baseline_sha`` is falsy, the commit-count / changed-paths probes
    are skipped (no git calls) and the evidence reports zero commits."""
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    executor._git_rev_parse_head = AsyncMock(return_value="h" * 40)  # type: ignore[method-assign]

    commit_count_called = False

    async def _commit_count(*_args: object, **_kwargs: object) -> int:  # pragma: no cover
        nonlocal commit_count_called
        commit_count_called = True
        return 5

    executor._git_commit_count_since = _commit_count  # type: ignore[method-assign]

    session = _CommittingSession()
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    events: list[str] = []

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _workspace_id: str) -> object:
            return SimpleNamespace(id="ws_stall")

        async def add_event(self, _ws: object, *, event_type: str, **_kwargs: object) -> None:
            events.append(event_type)

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)

    failure = await executor._build_conformance_stall_failure(  # noqa: SLF001
        workspace=SimpleNamespace(id="ws_stall"),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        baseline_sha=None,
        last_report=None,
        stall=_stall_evidence(),
        iterations_used=2,
        max_iterations=3,
        plan_path=Path("docs/plan.md"),
        report_path=Path("docs/report.json"),
        recovery_action=None,
    )

    assert failure.reason_code == AGENT_STALLED_IN_CONFORMANCE
    # baseline_sha falsy -> commit-count probe never runs and zero is reported.
    assert commit_count_called is False
    assert failure.details["conformance_stall"]["salvage_hint"]["implementation_commit_count"] == 0
    # No last_report -> the optional "conformance" details key is absent.
    assert "conformance" not in failure.details
    # Successful session records the stall event and commits.
    assert events == ["workspace.planning_conformance_stalled"]
    assert session.commits == 1


@pytest.mark.unit
async def test_build_conformance_stall_failure_records_event_with_persisted_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path records the stall event when the workspace is still
    persisted, committing once."""
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    executor._git_rev_parse_head = AsyncMock(return_value="h" * 40)  # type: ignore[method-assign]
    executor._git_commit_count_since = AsyncMock(return_value=3)  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(  # type: ignore[method-assign]
        return_value={Path("src/app.py")}
    )

    session = _CommittingSession()
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    events: list[tuple[str, str]] = []

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _workspace_id: str) -> object:
            return SimpleNamespace(id="ws_stall")

        async def add_event(
            self, _ws: object, *, event_type: str, reason_code: str, **_kwargs: object
        ) -> None:
            events.append((event_type, reason_code))

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)

    failure = await executor._build_conformance_stall_failure(  # noqa: SLF001
        workspace=SimpleNamespace(id="ws_stall"),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        baseline_sha="b" * 40,
        last_report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="still missing validation",
            gaps=("rerun tests",),
        ),
        stall=_stall_evidence(),
        iterations_used=2,
        max_iterations=3,
        plan_path=Path("docs/plan.md"),
        report_path=Path("docs/report.json"),
        recovery_action="notify",
    )

    assert failure.reason_code == AGENT_STALLED_IN_CONFORMANCE
    assert failure.details["conformance_stall"]["salvage_hint"]["changed_paths"] == ["src/app.py"]
    assert failure.details["conformance"]["gaps"] == ["rerun tests"]
    assert events == [("workspace.planning_conformance_stalled", AGENT_STALLED_IN_CONFORMANCE)]
    assert session.commits == 1


@pytest.mark.unit
async def test_build_conformance_stall_failure_skips_event_when_workspace_vanished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the workspace row has vanished by the time the stall event would be
    recorded, no event is written and no commit occurs, but the failure is
    still returned."""
    runner = FakeCommandRunner()
    executor = _executor_with_runner(runner, tmp_path)
    executor._git_rev_parse_head = AsyncMock(return_value="h" * 40)  # type: ignore[method-assign]
    executor._git_commit_count_since = AsyncMock(return_value=1)  # type: ignore[method-assign]
    executor._committed_paths_since = AsyncMock(return_value=set())  # type: ignore[method-assign]

    session = _CommittingSession()
    executor._session_factory = lambda: session  # type: ignore[method-assign]

    class _WorkspaceRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _workspace_id: str) -> None:
            return None

        async def add_event(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
            raise AssertionError("must not record when the workspace is gone")

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)

    failure = await executor._build_conformance_stall_failure(  # noqa: SLF001
        workspace=SimpleNamespace(id="ws_gone"),  # type: ignore[arg-type]
        worktree_path=tmp_path / "worktree",
        baseline_sha="b" * 40,
        last_report=None,
        stall=_stall_evidence(),
        iterations_used=1,
        max_iterations=2,
        plan_path=Path("docs/plan.md"),
        report_path=Path("docs/report.json"),
        recovery_action=None,
    )

    assert failure.reason_code == AGENT_STALLED_IN_CONFORMANCE
    assert session.commits == 0
