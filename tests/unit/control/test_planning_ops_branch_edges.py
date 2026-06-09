"""Branch-edge coverage for executor planning-scope auto-retry helpers.

Mirrors the in-memory session/repo doubles used by
``test_executor_planning_auto_retry_transactions.py`` and the executor edge
suites, focusing on early-return guards, the ``getattr(session, "rollback")``
defensive branch when a session lacks rollback, the pure event-matching
helpers, and the fresh-vs-stale post-validation conformance report paths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import planning_ops as planning_ops
from awf.control.executor.types import _PlanningValidationHandoff
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    AGENT_STALLED_IN_CONFORMANCE,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    ConformanceStallEvidence,
    ConformanceStallKind,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.service.workspaces import WorkspaceRetrySourceRuntimeNotReleasedError

# ---------------------------------------------------------------------------
# Pure event-matching helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_event_is_blocked_for_reason_false_when_event_type_mismatches() -> None:
    """An event that is not a blocked marker (wrong event_type) does not match,
    exercising the early ``return False`` guard."""
    event = SimpleNamespace(
        event_type="workspace.retry_requested",
        reason_code=planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
        payload={"retry_after": "terminal_runtime_released"},
    )
    assert (
        planning_ops._planning_scope_auto_retry_event_is_blocked_for_reason(  # noqa: SLF001
            event,
            planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
        )
        is False
    )


@pytest.mark.unit
def test_event_is_blocked_for_reason_true_for_non_host_port_reason() -> None:
    """A matching blocked marker for a non-host-port reason short-circuits to
    True without consulting detail."""
    event = SimpleNamespace(
        event_type=planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
        reason_code=planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
        payload={"retry_after": "terminal_runtime_released"},
    )
    assert (
        planning_ops._planning_scope_auto_retry_event_is_blocked_for_reason(  # noqa: SLF001
            event,
            planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
        )
        is True
    )


@pytest.mark.unit
def test_host_port_block_detail_matches_falls_back_to_equality_for_non_mappings() -> None:
    """Non-mapping details are compared by equality (covers the fallback
    branch)."""
    assert (
        planning_ops._planning_scope_auto_retry_host_port_block_detail_matches(  # noqa: SLF001
            "same",
            "same",
        )
        is True
    )
    assert (
        planning_ops._planning_scope_auto_retry_host_port_block_detail_matches(  # noqa: SLF001
            "old",
            None,
        )
        is False
    )


@pytest.mark.unit
def test_event_is_resume_failed_false_when_event_none() -> None:
    assert (
        planning_ops._planning_scope_auto_retry_event_is_resume_failed(None) is False  # noqa: SLF001
    )


@pytest.mark.unit
def test_payload_returns_empty_dict_when_payload_not_mapping() -> None:
    event = SimpleNamespace(payload=["not", "a", "mapping"])
    assert planning_ops._planning_scope_auto_retry_payload(event) == {}  # noqa: SLF001


@pytest.mark.unit
async def test_latest_terminal_release_event_skips_non_matching_then_returns_match() -> None:
    """The scan loop skips a leading non-matching event and returns the first
    matching one, exercising the loop-continue arc."""

    class _ExecuteResult:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        def scalars(self) -> _ExecuteResult:
            return self

        def __iter__(self) -> Any:
            return iter(self._events)

    class _Session:
        def __init__(self, events: list[object]) -> None:
            self._events = events

        async def execute(self, _stmt: object) -> _ExecuteResult:
            return _ExecuteResult(self._events)

    # First event: an auto-retry blocked event whose payload references a
    # different source reason code -> does not match -> loop continues.
    non_matching = SimpleNamespace(
        event_type=planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
        payload={"source_reason_code": "SOME_OTHER_REASON"},
    )
    matching = SimpleNamespace(
        event_type=planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
        payload={"source_reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION},
    )

    result = await planning_ops._latest_planning_scope_auto_retry_terminal_release_event(  # noqa: SLF001
        _Session([non_matching, matching]),
        "ws1",
    )
    assert result is matching


# ---------------------------------------------------------------------------
# _request_planning_scope_auto_retry — defensive / early-return guards
# ---------------------------------------------------------------------------


class _NoRollbackSession:
    """Session double WITHOUT a ``rollback`` attribute, exercising the
    ``getattr(session, "rollback", None) is None`` defensive branch."""

    def __init__(self, *, terminal_events: list[object] | None = None) -> None:
        self.operations: list[str] = []
        self._terminal_events = terminal_events or []

    async def commit(self) -> None:
        self.operations.append("commit")

    async def execute(self, _stmt: object) -> Any:
        self.operations.append("event-scan")
        return _IterResult(self._terminal_events)


class _IterResult:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def scalars(self) -> _IterResult:
        return self

    def __iter__(self) -> Any:
        return iter(self._events)


@pytest.mark.unit
async def test_request_auto_retry_returns_when_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished workspace short-circuits before any retry attempt."""
    retry_called = False

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, _workspace_id: str) -> None:
            return None

    async def _retry(*_args: Any, **_kwargs: Any) -> object:
        nonlocal retry_called
        retry_called = True
        raise AssertionError("retry must not run for a missing workspace")

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(planning_ops, "retry_workspace_row", _retry)

    await planning_ops._request_planning_scope_auto_retry(  # noqa: SLF001
        _NoRollbackSession(),
        workspace_id="ws_missing",
        source_reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )
    assert retry_called is False


@pytest.mark.unit
async def test_request_auto_retry_source_runtime_block_without_rollback_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the session lacks ``rollback``, the source-runtime-not-released
    handler skips the rollback call and still records the blocked marker."""
    events: list[tuple[str, str]] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def get_for_update(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def add_event(
            self, _ws: object, *, event_type: str, reason_code: str, payload: dict[str, Any]
        ) -> None:
            del payload
            events.append((event_type, reason_code))

    async def _retry(_session: object, _workspace_id: str, **_kwargs: Any) -> object:
        raise WorkspaceRetrySourceRuntimeNotReleasedError(source_workspace_id="ws_block")

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(planning_ops, "retry_workspace_row", _retry)

    session = _NoRollbackSession()
    await planning_ops._request_planning_scope_auto_retry(  # noqa: SLF001
        session,
        workspace_id="ws_block",
        source_reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )
    assert "rollback" not in session.operations
    assert events == [
        (
            planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
            planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
        )
    ]


@pytest.mark.unit
async def test_request_auto_retry_host_port_conflict_without_rollback_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host-port-conflict handler also tolerates a session without
    ``rollback`` and records the host-port blocked marker."""
    events: list[tuple[str, str]] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def get_for_update(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id, task_policy={})

        async def add_event(
            self, _ws: object, *, event_type: str, reason_code: str, payload: dict[str, Any]
        ) -> None:
            del payload
            events.append((event_type, reason_code))

    async def _retry(_session: object, _workspace_id: str, **_kwargs: Any) -> object:
        raise planning_ops.WorkspaceCreateHostPortConflictError(
            host_port=8123,
            conflicting_workspace_id="ws_other",
        )

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(planning_ops, "retry_workspace_row", _retry)

    session = _NoRollbackSession()
    await planning_ops._request_planning_scope_auto_retry(  # noqa: SLF001
        session,
        workspace_id="ws_block",
        source_reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )
    assert "rollback" not in session.operations
    assert events == [
        (
            planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_EVENT_TYPE,  # noqa: SLF001
            planning_ops._PLANNING_SCOPE_AUTO_RETRY_HOST_PORT_CONFLICT_REASON_CODE,  # noqa: SLF001
        )
    ]


@pytest.mark.unit
async def test_request_auto_retry_failed_branch_without_rollback_and_workspace_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the retry-error branch, a session without ``rollback`` skips it, and
    if the workspace has since vanished the function returns before writing a
    failed event."""
    events: list[str] = []

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session
            self._first_get = True

        async def get(self, workspace_id: str) -> object | None:
            # First get (pre-retry) returns a workspace; the re-fetch after the
            # rollback returns None to exercise the post-rollback guard.
            if self._first_get:
                self._first_get = False
                return SimpleNamespace(id=workspace_id, task_policy={})
            return None

        async def add_event(self, *_args: Any, **kwargs: Any) -> None:  # pragma: no cover
            events.append(kwargs["event_type"])

    async def _retry(_session: object, _workspace_id: str, **_kwargs: Any) -> object:
        raise planning_ops.WorkspaceRetryError("cannot retry", detail={"reason": "busy"})

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    monkeypatch.setattr(planning_ops, "retry_workspace_row", _retry)

    session = _NoRollbackSession()
    await planning_ops._request_planning_scope_auto_retry(  # noqa: SLF001
        session,
        workspace_id="ws_gone",
        source_reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    )
    assert "rollback" not in session.operations
    assert events == []


@pytest.mark.unit
async def test_blocked_after_rollback_returns_when_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_record_planning_scope_auto_retry_blocked_after_retry_rollback`` exits
    when the re-locked workspace row is gone."""
    added = False

    class _WorkspaceRepo:
        async def get_for_update(self, _workspace_id: str) -> None:
            return None

        async def add_event(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            nonlocal added
            added = True

    session = _NoRollbackSession()
    await planning_ops._record_planning_scope_auto_retry_blocked_after_retry_rollback(  # noqa: SLF001
        session,
        _WorkspaceRepo(),
        workspace_id="ws_gone",
        source_reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        reason_code=planning_ops._PLANNING_SCOPE_AUTO_RETRY_BLOCKED_REASON_CODE,  # noqa: SLF001
        detail={"source_workspace_id": "ws_gone"},
    )
    assert added is False


# ---------------------------------------------------------------------------
# _record_planning_scope_auto_retry_resume_failed_after_runtime_release guards
# ---------------------------------------------------------------------------


class _ResumeFailedSession:
    def __init__(self, *, terminal_events: list[object]) -> None:
        self.operations: list[str] = []
        self._terminal_events = terminal_events

    async def __aenter__(self) -> _ResumeFailedSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.operations.append("commit")

    async def execute(self, _stmt: object) -> Any:
        self.operations.append("event-scan")
        return _IterResult(self._terminal_events)


@pytest.mark.unit
async def test_resume_failed_returns_when_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, _workspace_id: str) -> None:
            return None

        async def add_event(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("must not record for a missing workspace")

    sessions: list[_ResumeFailedSession] = []

    def _session_factory() -> _ResumeFailedSession:
        session = _ResumeFailedSession(terminal_events=[])
        sessions.append(session)
        return session

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    executor = SimpleNamespace(_session_factory=_session_factory)

    await planning_ops._record_planning_scope_auto_retry_resume_failed_after_runtime_release(  # noqa: SLF001
        executor,
        workspace_id="ws_missing",
        error=RuntimeError("boom"),
    )
    # Returned before scanning events / committing.
    assert sessions[-1].operations == []


@pytest.mark.unit
async def test_resume_failed_returns_when_no_pending_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the latest terminal-release event is not a pending block (e.g. a
    retry was already requested), no resume-failed marker is written."""
    requested = SimpleNamespace(
        event_type="workspace.retry_requested",
        payload={"source_reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION},
    )

    class _WorkspaceRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get_for_update(self, workspace_id: str) -> object:
            return SimpleNamespace(id=workspace_id)

        async def add_event(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
            raise AssertionError("must not record when no pending block exists")

    sessions: list[_ResumeFailedSession] = []

    def _session_factory() -> _ResumeFailedSession:
        session = _ResumeFailedSession(terminal_events=[requested])
        sessions.append(session)
        return session

    monkeypatch.setattr(planning_ops, "WorkspaceRepository", _WorkspaceRepo)
    executor = SimpleNamespace(_session_factory=_session_factory)

    await planning_ops._record_planning_scope_auto_retry_resume_failed_after_runtime_release(  # noqa: SLF001
        executor,
        workspace_id="ws_no_block",
        error=RuntimeError("boom"),
    )
    assert sessions[-1].operations == ["event-scan"]


# ---------------------------------------------------------------------------
# _run_post_validation_conformance_check — fresh report path (skips re-write)
# ---------------------------------------------------------------------------


class _ReportWritingAdapter:
    """Adapter that writes a fresh satisfied report to disk during its run so
    ``report_from_fresh_file`` is True."""

    def __init__(self, *, report_abs_path: Path, content: str) -> None:
        self._report_abs_path = report_abs_path
        self._content = content
        self.prompts: list[str] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        self._report_abs_path.parent.mkdir(parents=True, exist_ok=True)
        self._report_abs_path.write_text(self._content, encoding="utf-8")
        # Empty stdout so the on-disk fresh report is the source of truth.
        return SimpleNamespace(stdout="", stderr="")


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


@pytest.mark.unit
async def test_post_validation_conformance_uses_fresh_on_disk_report_and_skips_rewrite(
    tmp_path: Path,
) -> None:
    """When the conformance rerun writes a fresh satisfied report, AWF does not
    re-synthesize the file and proceeds straight to commit + event."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_abs = worktree_path / report_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    # Sequenced runner outputs for the git calls the function makes:
    runner.queue_result(returncode=0, stdout="")  # before_compare (changed paths)
    runner.queue_result(returncode=0, stdout="head-before\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare (changed paths)
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    write_calls: list[object] = []

    def _no_rewrite(**kwargs: object) -> None:  # pragma: no cover - must not run
        write_calls.append(kwargs)
        raise AssertionError("fresh report must not be re-written")

    executor._write_satisfied_post_validation_conformance_report = _no_rewrite  # type: ignore[method-assign]
    committed: list[str] = []

    async def _commit(**kwargs: object) -> bool:
        committed.append(str(kwargs.get("validation_run_id")))
        return True

    executor._commit_post_validation_conformance_report = _commit  # type: ignore[method-assign]
    recorded: list[str] = []

    async def _record_event(**kwargs: object) -> None:
        recorded.append(str(kwargs.get("validation_run_id")))

    executor._record_post_validation_conformance_event = _record_event  # type: ignore[method-assign]

    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_post.md"),
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_ReportWritingAdapter(report_abs_path=report_abs, content=satisfied),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
    )

    assert failure is None
    assert write_calls == []  # 345->357: fresh report -> skip re-write
    assert committed == ["validation-run-1"]
    assert recorded == ["validation-run-1"]


# ---------------------------------------------------------------------------
# _build_conformance_stall_failure — no-baseline and successful event record
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _run_agent_task_with_optional_planning — timeout falls back to stdout report
# ---------------------------------------------------------------------------


class _StdoutTimeoutAdapter:
    """First two runs (plan + execute) succeed; the conformance run idles out
    with a satisfied report only in stdout (nothing written to disk), so the
    timeout branch falls back to ``report_text = stdout``."""

    def __init__(self, *, satisfied_stdout: str) -> None:
        self._satisfied_stdout = satisfied_stdout
        self.prompts: list[str] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        if len(self.prompts) == 3:  # conformance call
            raise AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(
                    returncode=124,
                    stdout=self._satisfied_stdout,
                    stderr="idle timeout",
                ),
                reason_code="AGENT_IDLE_TIMEOUT",
            )
        if len(self.prompts) == 1:
            return SimpleNamespace(stdout="plan written", stderr="")
        return SimpleNamespace(stdout="implementation", stderr="")


@pytest.mark.unit
async def test_planning_conformance_timeout_falls_back_to_stdout_report(
    tmp_path: Path,
) -> None:
    """On an idle timeout with no fresh on-disk report, the satisfied report is
    taken from stdout, short-circuiting the loop with success."""
    worktree = tmp_path / "worktree"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")  # before_plan changed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_stdout.md\n")  # plan dirty
    runner.queue_result(returncode=0, stdout="")  # committed paths
    runner.queue_result(returncode=0, stdout="sha1\n")  # implementation baseline HEAD
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_stdout.md\n")  # before_compare
    runner.queue_result(returncode=0, stdout="?? docs/awf-plans/ws_stdout.md\n")  # after_compare
    runner.queue_result(returncode=0, stdout="sha2\n")  # after_head
    executor = _executor_with_runner(runner, tmp_path)

    profile = WorkspaceProfile.model_validate(
        {
            "name": "planning-stdout",
            "planning": {
                "required": True,
                "plan_path": "docs/awf-plans/{workspace_id}.md",
                "conformance_report_path": "docs/awf-plans/{workspace_id}.json",
                "max_iterations": 0,
            },
        }
    )
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    result = await executor._run_agent_task_with_optional_planning(  # noqa: SLF001
        adapter=_StdoutTimeoutAdapter(satisfied_stdout=satisfied),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_stdout", task_prompt="do it"),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree,
        model=None,
    )

    # Satisfied report parsed from stdout -> conformance succeeds (returns None).
    assert result is None
