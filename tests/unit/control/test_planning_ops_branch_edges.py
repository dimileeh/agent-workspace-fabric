"""Branch-edge coverage for executor planning-scope auto-retry helpers.

Mirrors the in-memory session/repo doubles used by
``test_executor_planning_auto_retry_transactions.py`` and the executor edge
suites, focusing on early-return guards, the ``getattr(session, "rollback")``
defensive branch when a session lacks rollback, the pure event-matching
helpers, and the fresh-vs-stale post-validation conformance report paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import planning_conformance as planning_conformance
from awf.control.executor import planning_ops as planning_ops
from awf.control.executor.types import _PlanningValidationHandoff
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from awf.service import artifacts as executor_service_artifacts
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
    re-synthesize the file and proceeds straight to recording the event (the
    report is intentionally never committed — its path is gitignored, #544)."""
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
    # The report path is gitignored/untracked, so git restore fails; AWF falls
    # back to unlinking the fresh on-disk report (#604).
    runner.queue_result(returncode=1, stdout="", stderr="error: path not tracked")

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    write_calls: list[object] = []

    def _no_rewrite(**kwargs: object) -> None:  # pragma: no cover - must not run
        write_calls.append(kwargs)
        raise AssertionError("fresh report must not be re-written")

    executor._write_satisfied_post_validation_conformance_report = _no_rewrite  # type: ignore[method-assign]
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
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert write_calls == []  # fresh report -> skip re-write
    assert recorded == ["validation-run-1"]
    # The report is recorded as an event but never staged or committed.
    assert all("commit" not in call.args for call in runner.calls)
    # #604: the fresh on-worktree report is also removed so it cannot dirty the
    # tree during later validation/push cleanliness checks.
    assert not report_abs.exists()


@pytest.mark.unit
async def test_post_validation_conformance_prompt_anchors_agent_facing_paths(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6LBm4k regression: the post-validation conformance rerun
    must hand the agent worktree-root-anchored ``/workspace/...`` plan/report
    paths (like the initial loop, #620) so a rerun from a task subdir cannot
    write the report under ``apps/console/docs/awf-plans/...`` and trip the
    scope check. Internal scope logic still uses the relative handoff paths."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    plan_path = Path("docs/awf-plans/ws_post.md")
    worktree_path = tmp_path / "worktree"
    report_abs = worktree_path / report_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="head-before\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=1, stdout="", stderr="error: path not tracked")

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    async def _record_event(**kwargs: object) -> None:
        return None

    executor._record_post_validation_conformance_event = _record_event  # type: ignore[method-assign]

    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=plan_path,
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    adapter = _ReportWritingAdapter(report_abs_path=report_abs, content=satisfied)
    failure = await executor._run_post_validation_conformance_check(
        adapter=adapter,  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert len(adapter.prompts) == 1
    prompt = adapter.prompts[0]
    # The agent is instructed to read the plan and write the report at the
    # worktree-root-anchored paths, immune to the agent's CWD.
    assert "`/workspace/docs/awf-plans/ws_post.md`" in prompt
    assert "`/workspace/docs/awf-plans/ws_post.conformance.json`" in prompt


@pytest.mark.unit
async def test_post_validation_conformance_missing_artifact_root_skips_deposit(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KyDg- regression: satisfied conformance should not fail
    when a focused executor double lacks the best-effort artifact root config."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_abs = worktree_path / report_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="head-before\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=1, stdout="", stderr="error: path not tracked")

    executor = _executor_with_runner(runner, tmp_path)
    executor._config = SimpleNamespace(  # type: ignore[assignment]
        max_validation_fix_passes=0,
        planning_max_iterations_default=6,
    )
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

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

    with structlog.testing.capture_logs() as captured:
        failure = await executor._run_post_validation_conformance_check(
            adapter=_ReportWritingAdapter(report_abs_path=report_abs, content=satisfied),  # type: ignore[arg-type]
            workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
            profile=profile,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            worktree_path=worktree_path,
            model=None,
            handoff=handoff,
            validation_run_id="validation-run-1",
            base_commit="base-commit-sha",
        )

    assert failure is None
    assert recorded == ["validation-run-1"]
    assert not report_abs.exists()
    assert any(
        entry["event"]
        == "executor.post_validation_conformance_deposit_skipped_missing_artifact_root"
        and entry["workspace_id"] == "ws_post"
        for entry in captured
    )


@pytest.mark.unit
async def test_post_validation_conformance_stale_report_with_failed_rewrite_uses_in_memory_deposit(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KL7-o regression: when stdout supplies the satisfied
    report but the AWF-synthesized rewrite fails while a stale handoff report
    remains on disk, the served artifact must receive the in-memory satisfied
    report, not the stale disk copy."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    plan_path = Path("docs/awf-plans/ws_post.md")
    worktree_path = tmp_path / "worktree"
    report_abs = worktree_path / report_path
    plan_abs = worktree_path / plan_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="validated-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=128, stderr="fatal: path not in index\n"
    )  # git restore fails; fallback to unlink

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    async def _record_event(**_kwargs: object) -> None:
        return None

    executor._record_post_validation_conformance_event = _record_event  # type: ignore[method-assign]

    # The worktree carries a stale unsatisfied report, but the conformance call
    # only emits the satisfied report in stdout.
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# plan\n", encoding="utf-8")
    report_abs.parent.mkdir(parents=True, exist_ok=True)
    stale_content = '{"status":"needs_iteration","summary":"stale unsatisfied","gaps":["do work"]}'
    report_abs.write_text(stale_content, encoding="utf-8")

    def _fail_rewrite(**_kwargs: object) -> None:
        raise OSError("disk full")

    executor._write_satisfied_post_validation_conformance_report = _fail_rewrite  # type: ignore[method-assign]

    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=plan_path,
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(satisfied),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(
        tmp_path / "compose" / "..", "ws_post"
    ).resolve()
    deposited_report = json.loads((artifact_dir / "conformance.json").read_text(encoding="utf-8"))
    assert deposited_report["status"] == "satisfied"
    assert deposited_report["summary"] == "validated evidence satisfies plan"
    assert deposited_report["gaps"] == []


class _PlanningAdapter:
    def __init__(self, *stdout_values: str) -> None:
        self.stdout_values = list(stdout_values)
        self.prompts: list[str] = []

    async def run(self, **kwargs: object) -> SimpleNamespace:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        stdout = self.stdout_values.pop(0) if self.stdout_values else ""
        return SimpleNamespace(stdout=stdout, stderr="")


@pytest.mark.unit
def test_empty_report_parent_residue_treats_oserror_as_dirty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AWF cannot inspect an otherwise empty report parent, treat it as
    dirty so cleanup residue cannot be silently ignored."""
    worktree_path = tmp_path / "worktree"
    report_parent = worktree_path / "docs" / "awf-plans"
    report_parent.mkdir(parents=True)

    real_iterdir = Path.iterdir

    def _raise_on_report_parent(self: Path) -> Any:
        if self == report_parent:
            raise OSError(5, "I/O error")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _raise_on_report_parent)

    assert (
        planning_conformance._empty_report_parent_residue_is_dirty(  # noqa: SLF001
            report_parent,
            worktree_path=worktree_path,
        )
        is True
    )


@pytest.mark.unit
def test_remove_stale_satisfied_conformance_artifacts_logs_unlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale served conformance artifacts are best-effort cleanup; unlink
    failures should preserve diagnostic logs and not fail the deposit path."""
    artifact_dir = tmp_path / "artifacts" / "ws_deposit"
    dest = artifact_dir / "conformance.json"
    tmp_dest = artifact_dir / ".conformance.json.tmp"
    artifact_dir.mkdir(parents=True)
    dest.write_text('{"status":"needs_iteration"}\n', encoding="utf-8")
    tmp_dest.write_text('{"status":"needs_iteration"}\n', encoding="utf-8")

    def _raise_on_stale_artifact(self: Path, missing_ok: bool = False) -> None:
        if self in {dest, tmp_dest}:
            raise OSError(13, "Permission denied")
        raise AssertionError(f"unexpected unlink for {self}")

    monkeypatch.setattr(Path, "unlink", _raise_on_stale_artifact)

    with structlog.testing.capture_logs() as captured:
        planning_conformance._remove_stale_satisfied_conformance_artifacts(  # noqa: SLF001
            workspace_id="ws_deposit",
            dest=dest,
            tmp_dest=tmp_dest,
        )

    cleanup_failures = [
        entry
        for entry in captured
        if entry["event"] == "executor.satisfied_conformance_report_deposit_cleanup_failed"
    ]
    assert [
        (entry["workspace_id"], entry["artifact_name"], entry["error_type"], entry["errno"])
        for entry in cleanup_failures
    ] == [
        ("ws_deposit", "conformance.json", "PermissionError", 13),
        ("ws_deposit", ".conformance.json.tmp", "PermissionError", 13),
    ]
    assert dest.exists()
    assert tmp_dest.exists()


@pytest.mark.unit
def test_deposit_satisfied_conformance_report_mkdir_oserror_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating the served artifact directory is best-effort; a filesystem
    failure should be logged without failing the satisfied conformance outcome."""
    work_dir = tmp_path / "work_dir"
    worktree_path = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_deposit.md")
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="validated evidence satisfies plan",
        gaps=(),
    )
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(work_dir, "ws_deposit")
    real_mkdir = Path.mkdir

    def _raise_on_artifact_dir_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == artifact_dir:
            raise OSError(13, "Permission denied")
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _raise_on_artifact_dir_mkdir)

    with structlog.testing.capture_logs() as captured:
        planning_ops._deposit_satisfied_conformance_report(
            work_dir=work_dir,
            workspace_id="ws_deposit",
            worktree_path=worktree_path,
            plan_path=plan_path,
            report=report,
        )

    assert not artifact_dir.exists()
    assert any(
        entry["event"] == "executor.satisfied_conformance_report_deposit_failed"
        and entry["workspace_id"] == "ws_deposit"
        and entry["error_type"] == "PermissionError"
        and entry["errno"] == 13
        for entry in captured
    )


@pytest.mark.unit
async def test_post_validation_conformance_unlink_failure_is_non_fatal(
    tmp_path: Path,
) -> None:
    """#604: if the satisfied report cannot be removed from the worktree, the
    failure must be best-effort and non-fatal; the conformance outcome is still
    recorded and success is returned."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_abs = worktree_path / report_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="head-before\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    runner.queue_result(returncode=1, stdout="", stderr="restore failed")  # git restore report path

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    async def _record_event(**_kwargs: object) -> None:
        return None

    executor._record_post_validation_conformance_event = _record_event  # type: ignore[method-assign]

    real_unlink = Path.unlink

    def _raise_on_report_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == report_abs and missing_ok:
            raise OSError("permission denied")
        return real_unlink(self, missing_ok=missing_ok)

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

    # Ensure the report file exists before the conformance check so that a
    # mocked ``Path.unlink`` raises on the actual unlink call rather than on
    # the write failure path (which would make the file absent and leave
    # ``missing_ok=True`` a no-op).
    report_abs.parent.mkdir(parents=True, exist_ok=True)
    report_abs.write_text(satisfied, encoding="utf-8")

    with patch.object(Path, "unlink", _raise_on_report_unlink):
        failure = await executor._run_post_validation_conformance_check(
            adapter=_PlanningAdapter(satisfied),  # type: ignore[arg-type]
            workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
            profile=profile,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            worktree_path=worktree_path,
            model=None,
            handoff=handoff,
            validation_run_id="validation-run-1",
            base_commit="base-commit-sha",
        )

    assert failure is None


@pytest.mark.unit
async def test_post_validation_conformance_report_deposit_oserror_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6KL4ab regression: an OSError depositing the served
    satisfied conformance report must not propagate and fail the
    post-validation conformance check."""
    work_dir = tmp_path / "work_dir"
    worktree_path = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_deposit.md")
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="validated evidence satisfies plan",
        gaps=(),
    )

    artifact_dir = work_dir / "artifacts" / "ws_deposit"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "conformance.json").write_text(
        '{"status":"needs_iteration","summary":"stale","gaps":["old gap"]}\n',
        encoding="utf-8",
    )
    (artifact_dir / ".conformance.json.tmp").write_text(
        '{"status":"needs_iteration","summary":"stale tmp","gaps":["old gap"]}\n',
        encoding="utf-8",
    )

    # Ensure the artifact directory parent exists so mkdir passes; block only
    # the temporary report rewrite so we exercise the new error path.
    real_write_text = Path.write_text

    def _raise_on_report_tmp(self: Path, content: str, *, encoding: str | None = None) -> int:
        if self.name == ".conformance.json.tmp":
            raise OSError(13, "Permission denied")
        return real_write_text(self, content, encoding=encoding)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _raise_on_report_tmp)

    # Make sure the best-effort plan copy still runs after the report write
    # failure so callers can serve the plan even when the convenience
    # conformance artifact cannot be rewritten.
    plan_source = worktree_path / plan_path
    plan_source.parent.mkdir(parents=True, exist_ok=True)
    plan_source.write_text("# plan", encoding="utf-8")

    with structlog.testing.capture_logs() as captured:
        planning_ops._deposit_satisfied_conformance_report(
            work_dir=work_dir,
            workspace_id="ws_deposit",
            worktree_path=worktree_path,
            plan_path=plan_path,
            report=report,
        )

    # The served report and temp report are absent because the rewrite failed.
    assert not (artifact_dir / "conformance.json").exists()
    assert not (artifact_dir / ".conformance.json.tmp").exists()
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan"

    assert any(
        entry["event"] == "executor.satisfied_conformance_report_deposit_failed"
        and entry["workspace_id"] == "ws_deposit"
        and entry["error_type"] == "PermissionError"
        and entry["errno"] == 13
        for entry in captured
    )


@pytest.mark.unit
def test_deposit_satisfied_conformance_report_rejects_symlinked_plan(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KMbis regression: the fallback satisfied-conformance
    deposit must not follow a symlinked plan into host-readable files."""
    work_dir = tmp_path / "work_dir"
    worktree_path = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_deposit.md")
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="validated evidence satisfies plan",
        gaps=(),
    )

    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (worktree_path / plan_path.parent).mkdir(parents=True, exist_ok=True)
    (worktree_path / plan_path).symlink_to(secret)

    with structlog.testing.capture_logs() as captured:
        planning_ops._deposit_satisfied_conformance_report(
            work_dir=work_dir,
            workspace_id="ws_deposit",
            worktree_path=worktree_path,
            plan_path=plan_path,
            report=report,
        )

    artifact_dir = executor_service_artifacts.workspace_artifact_dir(work_dir, "ws_deposit")
    assert (artifact_dir / "conformance.json").exists()
    assert not (artifact_dir / "plan.md").exists()
    assert any(
        entry["event"] == "service.planning_artifact_deposit_rejected"
        and entry["workspace_id"] == "ws_deposit"
        and entry["reason"] == "symlink"
        for entry in captured
    )


@pytest.mark.unit
def test_deposit_satisfied_conformance_report_rejects_plan_escaping_worktree(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KMbis regression: a plan reached via an intermediate
    directory symlink outside the worktree must not be deposited."""
    work_dir = tmp_path / "work_dir"
    worktree_path = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_deposit.md")
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="validated evidence satisfies plan",
        gaps=(),
    )

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / plan_path.name).write_text("# outside plan", encoding="utf-8")

    plans_dir = worktree_path / plan_path.parent
    plans_dir.parent.mkdir(parents=True, exist_ok=True)
    plans_dir.symlink_to(outside_dir, target_is_directory=True)

    with structlog.testing.capture_logs() as captured:
        planning_ops._deposit_satisfied_conformance_report(
            work_dir=work_dir,
            workspace_id="ws_deposit",
            worktree_path=worktree_path,
            plan_path=plan_path,
            report=report,
        )

    artifact_dir = executor_service_artifacts.workspace_artifact_dir(work_dir, "ws_deposit")
    assert (artifact_dir / "conformance.json").exists()
    assert not (artifact_dir / "plan.md").exists()
    assert any(
        entry["event"] == "service.planning_artifact_deposit_rejected"
        and entry["workspace_id"] == "ws_deposit"
        and entry["reason"] == "escapes_worktree"
        for entry in captured
    )


@pytest.mark.unit
def test_deposit_satisfied_conformance_report_rejects_oversized_report(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KxUlq regression: the stdout-derived fallback report
    deposit must not write artifacts larger than the served artifact cap.

    PRRT_kwDOSJAM6s6KznT8 regression: rejecting an oversized report must still
    deposit the safe plan artifact through the hardened plan copy path.
    """
    work_dir = tmp_path / "work_dir"
    worktree_path = tmp_path / "worktree"
    plan_path = Path("docs/awf-plans/ws_deposit.md")
    (worktree_path / plan_path.parent).mkdir(parents=True, exist_ok=True)
    (worktree_path / plan_path).write_text("# plan", encoding="utf-8")
    report = PlanConformanceReport(
        status=PlanConformanceStatus.satisfied,
        summary="x" * executor_service_artifacts.MAX_ARTIFACT_CONTENT_BYTES,
        gaps=(),
    )
    artifact_dir = executor_service_artifacts.workspace_artifact_dir(work_dir, "ws_deposit")
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "conformance.json").write_text(
        '{"status":"needs_iteration","summary":"stale","gaps":["old gap"]}\n',
        encoding="utf-8",
    )

    with structlog.testing.capture_logs() as captured:
        planning_ops._deposit_satisfied_conformance_report(
            work_dir=work_dir,
            workspace_id="ws_deposit",
            worktree_path=worktree_path,
            plan_path=plan_path,
            report=report,
        )

    assert not (artifact_dir / "conformance.json").exists()
    assert not (artifact_dir / ".conformance.json.tmp").exists()
    assert (artifact_dir / "plan.md").read_text(encoding="utf-8") == "# plan"
    assert any(
        entry["event"] == "executor.satisfied_conformance_report_deposit_rejected"
        and entry["workspace_id"] == "ws_deposit"
        and entry["reason"] == "oversized"
        for entry in captured
    )


@pytest.mark.unit
async def test_post_validation_conformance_staged_deletion_restored_from_head(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6KKSZU regression: when ``base_commit`` lacks the report
    but HEAD contains it, ``git restore --source=base_commit`` stages a
    deletion. The executor must restore from HEAD so the committed report copy
    remains on disk and the index is clean."""
    runner = FakeCommandRunner()
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    worktree_path = tmp_path / "worktree"
    report_abs = worktree_path / report_path
    satisfied = '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'

    runner.queue_result(returncode=0, stdout="")  # before_compare
    runner.queue_result(returncode=0, stdout="head-before\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout="")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed_paths_since
    # base_commit restore exits 0 but leaves staged deletion relative to HEAD.
    runner.queue_result(returncode=0, stdout=f"D  {report_path.as_posix()}\n")
    # Cleanliness check after base_commit restore still sees the staged deletion.
    runner.queue_result(returncode=0, stdout=f"D  {report_path.as_posix()}\n")
    # HEAD restore exits 0 and cleans the path.
    runner.queue_result(returncode=0, stdout="")
    # Final cleanliness check after HEAD restore is clean.
    runner.queue_result(returncode=0, stdout="")

    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    async def _record_event(**_kwargs: object) -> None:
        return None

    executor._record_post_validation_conformance_event = _record_event  # type: ignore[method-assign]

    # Preserve the committed HEAD copy on disk; the real writer would overwrite
    # it with the AWF-synthesized satisfied report.
    def _no_overwrite(**kwargs: object) -> None:
        del kwargs

    executor._write_satisfied_post_validation_conformance_report = _no_overwrite  # type: ignore[method-assign]

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

    # Simulate the committed HEAD copy by writing it to disk; the fake HEAD
    # restore command leaves the file untouched, but the cleanliness check
    # returns an empty status after it.
    report_abs.parent.mkdir(parents=True, exist_ok=True)
    report_abs.write_text('{"status":"committed","summary":"from head"}', encoding="utf-8")

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(satisfied),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert report_abs.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source HEAD --worktree --staged -- "
        ":(literal)docs/awf-plans/ws_post.conformance.json" in call
        for call in joined_calls
    )
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)
