"""Core runtime setup replay: identity capture, recreation decision, replay (#787, #1006)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.control.executor import runtime_setup_replay
from awf.control.executor.constants import (
    RUNTIME_IDENTITY_PROBE_TIMEOUT_REASON_CODE,
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
    RUNTIME_SETUP_REPLAYED_REASON_CODE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE,
)
from awf.control.executor.runtime_setup_replay import (
    IDENTITY_UNAVAILABLE,
    AgentContainerIdentity,
    RecreationDecision,
    capture_agent_container_identity,
    replay_setup_if_recreated,
    runtime_recreation_decision,
)
from awf.db.enums import FailureReason
from awf.db.models import WorkspaceEvent
from awf.db.session import make_session_factory
from awf.node.git_manager import GitOperationError
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
)
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
    SETUP_DEPENDENCY_NETWORK_RETRY,
    SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED,
    ValidationCommandResult,
    ValidationResult,
)
from awf.runtime.validation_types import ValidationCoverageResult
from tests.postgres import postgres_test_engine
from tests.unit.control.runtime_setup_replay_fakes import (
    SETUP_COMMAND,
    TOOL_PATH,
    FakeAgentContainer,
    FakeToolValidation,
    allow_ownership_repair,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)

_OLD = AgentContainerIdentity(container_id="c-old", started_at="2026-10-22T00:00:00Z")
_NEW = AgentContainerIdentity(container_id="c-new", started_at="2026-10-22T00:01:00Z")
_RESTARTED = AgentContainerIdentity(container_id="c-old", started_at="2026-10-22T00:02:00Z")
_SECRET = "ghp_" + "A" * 36


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _ownership_repair_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_setup_replay, "repair_agent_runtime_ownership", allow_ownership_repair
    )


async def _replay_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
            )
        )
        return list(rows.scalars())


async def _replay(
    factory: async_sessionmaker[AsyncSession],
    validation: Any,
    workspace_id: str,
    tmp_path: Path,
    *,
    before: Any,
    after: Any,
    recover_missing_head: Any = None,
) -> runtime_setup_replay.SetupReplayOutcome:
    return await replay_setup_if_recreated(
        session_factory=factory,
        validation=validation,
        workspace_id=workspace_id,
        compose_project="awf_x",
        compose_file=tmp_path / "compose.yml",
        profile=object(),
        worktree_path=tmp_path / "worktree",
        before=before,
        after=after,
        trigger="monitor_resume",
        ownership_event_name=EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        recover_missing_head=recover_missing_head,
    )


class TestRuntimeRecreationDecision:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("before", "after", "decision", "basis"),
        [
            (_OLD, _NEW, RecreationDecision.recreated, "identity_changed"),
            (_OLD, _RESTARTED, RecreationDecision.recreated, "identity_changed"),
            (None, _NEW, RecreationDecision.recreated, "agent_container_created"),
            (
                IDENTITY_UNAVAILABLE,
                _NEW,
                RecreationDecision.unknown,
                "previous_identity_unavailable",
            ),
            (_OLD, _OLD, RecreationDecision.unchanged, "identity_unchanged"),
            (_OLD, IDENTITY_UNAVAILABLE, RecreationDecision.unknown, "identity_unavailable"),
            (_OLD, None, RecreationDecision.unchanged, "no_agent_container"),
        ],
    )
    def test_truth_table(
        self, before: Any, after: Any, decision: RecreationDecision, basis: str
    ) -> None:
        verdict = runtime_recreation_decision(before, after)
        assert verdict.decision is decision
        assert verdict.basis == basis


class _Inspector:
    def __init__(self, snapshot: RuntimeSnapshot | None = None, error: Exception | None = None):
        self._snapshot = snapshot
        self._error = error
        self.projects: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.projects.append(compose_project_name)
        if self._error is not None:
            raise self._error
        assert self._snapshot is not None
        return self._snapshot


def _service(name: str, container_id: str | None = "c1", **kwargs: Any) -> RuntimeService:
    return RuntimeService(
        name=name,
        container_id=container_id,
        image=None,
        state="running",
        started_at="2026-10-22T00:00:00Z",
        **kwargs,
    )


class TestCaptureAgentContainerIdentity:
    @pytest.mark.unit
    async def test_picks_agent_service_identity(self) -> None:
        inspector = _Inspector(
            RuntimeSnapshot(
                stack_state="running",
                services=[_service("postgres", "db1"), _service("agent", "ag1")],
            )
        )
        identity = await capture_agent_container_identity("awf_x", inspector=inspector)
        assert identity == AgentContainerIdentity(
            container_id="ag1", started_at="2026-10-22T00:00:00Z"
        )
        assert inspector.projects == ["awf_x"]

    @pytest.mark.unit
    async def test_no_agent_container_returns_none(self) -> None:
        inspector = _Inspector(RuntimeSnapshot(stack_state="stopped", services=[]))
        assert await capture_agent_container_identity("awf_x", inspector=inspector) is None

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "snapshot",
        [
            RuntimeSnapshot(stack_state="unavailable", reason="docker down"),
            RuntimeSnapshot(stack_state="unknown"),
            RuntimeSnapshot(
                stack_state="running",
                services=[_service("unlabeled", service_label_available=False)],
            ),
            RuntimeSnapshot(stack_state="running", services=[_service("agent", None)]),
        ],
    )
    async def test_unreadable_identity_is_unavailable(self, snapshot: RuntimeSnapshot) -> None:
        inspector = _Inspector(snapshot)
        identity = await capture_agent_container_identity("awf_x", inspector=inspector)
        assert identity is IDENTITY_UNAVAILABLE

    @pytest.mark.unit
    async def test_inspect_error_is_unavailable(self) -> None:
        inspector = _Inspector(error=OSError("docker exploded"))
        identity = await capture_agent_container_identity("awf_x", inspector=inspector)
        assert identity is IDENTITY_UNAVAILABLE

    @pytest.mark.unit
    async def test_hung_docker_inspect_times_out_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged Docker CLI must not stall the resume/recovery that captures identity."""

        class _HungInspector:
            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        monkeypatch.setattr(runtime_setup_replay, "IDENTITY_PROBE_TIMEOUT_SECONDS", 0.01)

        with capture_logs() as logs:
            identity = await capture_agent_container_identity(
                "awf_x", inspector=cast(Any, _HungInspector())
            )

        assert identity is IDENTITY_UNAVAILABLE
        timeouts = [log for log in logs if log["event"] == "executor.runtime_identity_unavailable"]
        assert [log["reason_code"] for log in timeouts] == [
            RUNTIME_IDENTITY_PROBE_TIMEOUT_REASON_CODE
        ]
        assert timeouts[0]["timeout_seconds"] == 0.01

    @pytest.mark.unit
    async def test_default_inspector_is_runtime_inspector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inspector = _Inspector(RuntimeSnapshot(stack_state="stopped", services=[]))
        monkeypatch.setattr(runtime_setup_replay, "RuntimeInspector", lambda: inspector)
        assert await capture_agent_container_identity("awf_x") is None
        assert inspector.projects == ["awf_x"]


class TestReplaySetupIfRecreated:
    @pytest.mark.unit
    async def test_unchanged_identity_does_not_replay(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_OLD)

        assert outcome.ok and not outcome.replayed
        assert validation.phase_calls == []
        assert validation.probe_calls == 0
        assert await _replay_events(factory, ws_id) == []

    @pytest.mark.unit
    async def test_recreation_replays_setup_once_and_records_event(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        container = FakeAgentContainer()
        validation = FakeToolValidation(container)

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert outcome.ok and outcome.replayed
        assert validation.phase_calls == [("setup", "pre_agent")]
        assert TOOL_PATH in container.tmp
        events = await _replay_events(factory, ws_id)
        assert len(events) == 1
        assert events[0].reason_code == RUNTIME_SETUP_REPLAYED_REASON_CODE
        assert events[0].payload == {
            "trigger": "monitor_resume",
            "basis": "identity_changed",
            "container_id": "c-new",
            "started_at": "2026-10-22T00:01:00Z",
            "previous_container_id": "c-old",
            "phase_names": ["setup", "pre_agent"],
        }

    @pytest.mark.unit
    async def test_same_recreation_replays_only_once(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())

        first = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)
        # A second observer of the same recreation (e.g. the "before" probe hiccuped)
        # must not run setup again or append a second event.
        second = await _replay(
            factory, validation, ws_id, tmp_path, before=IDENTITY_UNAVAILABLE, after=_NEW
        )

        assert first.replayed and second.ok and not second.replayed
        assert validation.setup_runs() == 1
        assert validation.probe_calls == 0
        assert len(await _replay_events(factory, ws_id)) == 1

    @pytest.mark.unit
    async def test_a_later_recreation_replays_again(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())

        await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)
        await _replay(factory, validation, ws_id, tmp_path, before=_NEW, after=_RESTARTED)

        assert validation.setup_runs() == 2
        assert len(await _replay_events(factory, ws_id)) == 2

    @pytest.mark.unit
    async def test_unknown_identity_replays_even_when_validate_tools_resolve(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        # Resolving validate executables (``python`` for ``python -m pytest``) does
        # not prove setup-installed packages survived a possible recreation.
        ws_id = await _seed_monitoring_pr(factory)
        container = FakeAgentContainer()
        container.tmp.add(TOOL_PATH)
        validation = FakeToolValidation(container)

        outcome = await _replay(
            factory, validation, ws_id, tmp_path, before=_OLD, after=IDENTITY_UNAVAILABLE
        )

        assert outcome.ok and outcome.replayed
        assert validation.probe_calls == 0
        assert validation.phase_calls == [("setup", "pre_agent")]
        events = await _replay_events(factory, ws_id)
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["basis"] == "identity_unavailable"
        assert events[0].payload["container_id"] is None

    @pytest.mark.unit
    async def test_unknown_previous_identity_replays_once_even_when_validate_tools_resolve(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        # Only the pre-up read failed: the post-up container may or may not be the
        # initially provisioned one, so setup replays once and the event dedupes.
        ws_id = await _seed_monitoring_pr(factory)
        container = FakeAgentContainer()
        container.tmp.add(TOOL_PATH)
        validation = FakeToolValidation(container)

        first = await _replay(
            factory, validation, ws_id, tmp_path, before=IDENTITY_UNAVAILABLE, after=_NEW
        )
        second = await _replay(
            factory, validation, ws_id, tmp_path, before=IDENTITY_UNAVAILABLE, after=_NEW
        )

        assert first.ok and first.replayed
        assert second.ok and not second.replayed
        assert validation.probe_calls == 0
        assert validation.phase_calls == [("setup", "pre_agent")]
        events = await _replay_events(factory, ws_id)
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["basis"] == "previous_identity_unavailable"
        assert events[0].payload["container_id"] == "c-new"
        assert events[0].payload["previous_container_id"] is None

    @pytest.mark.unit
    async def test_failed_setup_surfaces_reason_code_and_redacted_output(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(
            FakeAgentContainer(),
            setup_returncode=2,
            setup_stdout=f"downloading with token={_SECRET}\ninstall failed: checksum mismatch",
        )

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok and outcome.replayed
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.failure_reason == FailureReason.service_startup_failure
        assert outcome.message is not None and SETUP_COMMAND in outcome.message
        details = outcome.details
        assert details is not None
        assert details["trigger"] == "monitor_resume"
        assert details["basis"] == "identity_changed"
        assert details["phase"] == "setup"
        assert details["command"] == SETUP_COMMAND
        assert details["returncode"] == 2
        assert "checksum mismatch" in details["stdout_tail"]
        assert _SECRET not in repr(details)
        assert _SECRET not in outcome.message
        assert validation.setup_runs() == 1
        assert await _replay_events(factory, ws_id) == []

    @pytest.mark.unit
    async def test_setup_exception_surfaces_reason_code_with_redacted_error(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(
            FakeAgentContainer(), setup_error=RuntimeError(f"exec failed token={_SECRET}")
        )

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=None, after=_NEW)

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.failure_reason == FailureReason.infrastructure_failure
        assert outcome.message is not None and "exec failed" in outcome.message
        assert _SECRET not in outcome.message
        assert outcome.details is not None
        assert outcome.details["basis"] == "agent_container_created"
        assert _SECRET not in repr(outcome.details)
        assert await _replay_events(factory, ws_id) == []

    @pytest.mark.unit
    async def test_compose_exec_cleanup_failure_keeps_its_reason_code(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(
            FakeAgentContainer(),
            setup_error=ComposeExecCleanupError(
                invocation_id="inv", source="validation", label="setup", message="stuck"
            ),
        )

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.failure_reason == FailureReason.infrastructure_failure
        assert outcome.details is not None
        assert outcome.details["trigger"] == "monitor_resume"
        assert outcome.details["cause_reason_code"] == EXEC_PROCESS_CLEANUP_FAILED

    @pytest.mark.unit
    async def test_ownership_repair_failure_skips_setup(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _repair_fails(**_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(runtime_setup_replay, "repair_agent_runtime_ownership", _repair_fails)
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.details is not None
        assert (
            outcome.details["cause_reason_code"]
            == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )
        assert validation.phase_calls == []

    @pytest.mark.unit
    async def test_missing_workspace_row_still_replays_without_event(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        validation = FakeToolValidation(FakeAgentContainer())

        outcome = await _replay(
            factory, validation, "ws_missing", tmp_path, before=_OLD, after=_NEW
        )

        assert outcome.ok and outcome.replayed
        assert validation.setup_runs() == 1


class _FixedResultValidation:
    """Validation double returning one canned setup result."""

    def __init__(self, result: ValidationResult) -> None:
        self._result = result
        self.calls = 0

    async def run_profile_phases(self, **_kwargs: Any) -> ValidationResult:
        self.calls += 1
        return self._result


class TestReplayFailureDetails:
    @pytest.mark.unit
    async def test_output_is_read_from_artifacts_and_dependency_details_are_kept(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        stdout_path = tmp_path / "setup.stdout"
        stdout_path.write_text(f"fetch with token={_SECRET}\nresolve failed\n", encoding="utf-8")
        failing = ValidationCommandResult(
            command="uv sync --extra dev",
            returncode=1,
            duration_seconds=0.1,
            stdout_path=stdout_path,
            stderr_path=tmp_path / "missing.stderr",
            phase="setup",
            reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE,
            metadata={
                SETUP_DEPENDENCY_NETWORK_METADATA_KEY: {
                    "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                    "host": "files.pythonhosted.org",
                    "retry_exhausted": True,
                }
            },
        )
        validation = _FixedResultValidation(ValidationResult(commands=[failing]))

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        details = outcome.details
        assert details is not None
        assert details["stdout_tail"].endswith("resolve failed\n")
        assert _SECRET not in details["stdout_tail"]
        assert details["stderr_tail"] == ""
        assert details["setup_dependency_network"]["host"] == "files.pythonhosted.org"

    @pytest.mark.unit
    async def test_failure_without_failing_command_still_surfaces(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        coverage_failure = ValidationCoverageResult(
            provider="pytest-cov",
            percent=None,
            minimum_percent=99.0,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_FAILED",
        )
        validation = _FixedResultValidation(ValidationResult(coverage=coverage_failure))

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.message == "runtime setup replay failed: profile setup failed"
        assert outcome.details == {"trigger": "monitor_resume", "basis": "identity_changed"}

    @pytest.mark.unit
    async def test_redaction_never_truncates_the_output_tail(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(
            FakeAgentContainer(),
            setup_returncode=1,
            setup_stdout="line1\nexport GITHUB_TOKEN=ab\nERROR: final failure line",
        )

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert outcome.details is not None
        tail = outcome.details["stdout_tail"]
        assert tail.endswith("ERROR: final failure line")
        assert "GITHUB_TOKEN=ab" not in tail


def _dependency_retry_command(tmp_path: Path, *, exhausted: bool) -> ValidationCommandResult:
    return ValidationCommandResult(
        command="uv sync --extra dev",
        returncode=1 if exhausted else 0,
        duration_seconds=0.1,
        stdout_path=tmp_path / "setup.stdout",
        stderr_path=tmp_path / "setup.stderr",
        phase="setup",
        reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE if exhausted else "VALIDATION_OK",
        retry_count=2 if exhausted else 1,
        metadata={
            SETUP_DEPENDENCY_NETWORK_METADATA_KEY: {
                "reason_code": SETUP_DEPENDENCY_NETWORK_FAILURE,
                "host": "files.pythonhosted.org",
                "retry_count": 2 if exhausted else 1,
                "retry_budget": 2,
                "retry_exhausted": exhausted,
                "diagnostic": f"lookup failed; Authorization: Bearer {_SECRET}",
            }
        },
    )


async def _events_of(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, event_type: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == event_type,
            )
        )
        return list(rows.scalars())


class TestReplayDependencyNetworkEvents:
    @pytest.mark.unit
    async def test_replay_recovered_by_retry_records_the_retry_event(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = _FixedResultValidation(
            ValidationResult(commands=[_dependency_retry_command(tmp_path, exhausted=False)])
        )

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert outcome.ok and outcome.replayed
        retries = await _events_of(factory, ws_id, SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE)
        assert [event.reason_code for event in retries] == [SETUP_DEPENDENCY_NETWORK_RETRY]
        assert _SECRET not in str(retries[0].payload)
        assert not await _events_of(
            factory, ws_id, SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE
        )

    @pytest.mark.unit
    async def test_replay_exhausting_retries_records_both_events_before_failing(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = _FixedResultValidation(
            ValidationResult(commands=[_dependency_retry_command(tmp_path, exhausted=True)])
        )

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert await _events_of(factory, ws_id, SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE)
        exhausted = await _events_of(
            factory, ws_id, SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE
        )
        assert [event.reason_code for event in exhausted] == [
            SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED
        ]

    @pytest.mark.unit
    async def test_retry_event_db_error_does_not_fail_the_replay(self, tmp_path: Path) -> None:
        validation = _FixedResultValidation(
            ValidationResult(commands=[_dependency_retry_command(tmp_path, exhausted=False)])
        )

        with capture_logs() as logs:
            outcome = await _replay(
                lambda: _BrokenSession(),
                validation,
                "ws_db_down",
                tmp_path,
                before=_OLD,
                after=_NEW,
            )

        assert outcome.ok and outcome.replayed
        assert any(
            log["event"] == "executor.runtime_setup_replay_dependency_network_event_record_failed"
            for log in logs
        )


class _MirrorHooks:
    """Record mirror hooks-path repairs in the validation trace; optionally fail one."""

    def __init__(self, trace: list[str], *, fail_on_call: int | None = None) -> None:
        self.trace = trace
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def repair(self, mirror_path: Path) -> bool:
        self.calls += 1
        self.trace.append(f"repair_hooks:{mirror_path.name}")
        if self.calls == self.fail_on_call:
            raise GitOperationError(
                operation="config.unset",
                returncode=1,
                stdout="",
                stderr="locked",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        return False


@pytest.fixture
def mirror_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    def _install(trace: list[str], *, fail_on_call: int | None = None) -> _MirrorHooks:
        hooks = _MirrorHooks(trace, fail_on_call=fail_on_call)
        monkeypatch.setattr(
            runtime_setup_replay, "mirror_path_for_worktree", lambda _wt: tmp_path / "mirror.git"
        )
        monkeypatch.setattr(runtime_setup_replay, "repair_mirror_hooks_path", hooks.repair)
        return hooks

    return _install


class TestReplayGitBracket:
    """The replay runs inside the agent runtime like execution_flow's setup, so it
    brackets setup with mirror hooks-path repair and recovers a missing HEAD after
    a Compose exec cleanup failure."""

    @pytest.mark.unit
    async def test_mirror_hooks_repaired_before_and_after_setup(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path, mirror_hooks: Any
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())
        mirror_hooks(validation.trace)

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert outcome.ok and outcome.replayed
        assert validation.trace == [
            "repair_hooks:mirror.git",
            "phases:setup,pre_agent",
            "repair_hooks:mirror.git",
        ]

    @pytest.mark.unit
    async def test_mirror_hooks_repaired_after_failed_setup(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path, mirror_hooks: Any
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer(), setup_returncode=1)
        mirror_hooks(validation.trace)

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.details is not None
        assert outcome.details["command"] == SETUP_COMMAND
        assert validation.trace[-1] == "repair_hooks:mirror.git"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("fail_on_call", "stage", "setup_runs"),
        [(1, "before_setup_replay", 0), (2, "after_setup_replay", 1)],
    )
    async def test_mirror_hooks_repair_failure_fails_replay(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        mirror_hooks: Any,
        fail_on_call: int,
        stage: str,
        setup_runs: int,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())
        mirror_hooks(validation.trace, fail_on_call=fail_on_call)

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.failure_reason == FailureReason.infrastructure_failure
        assert outcome.details is not None
        assert outcome.details["cause_reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert outcome.details["mirror_hooks_repair_stage"] == stage
        assert validation.setup_runs() == setup_runs
        assert await _replay_events(factory, ws_id) == []

    @pytest.mark.unit
    async def test_mirror_hooks_repair_os_error_fails_replay(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _repair_os_error(_mirror_path: Path) -> bool:
            raise PermissionError("mirror config unreadable")

        monkeypatch.setattr(
            runtime_setup_replay, "mirror_path_for_worktree", lambda _wt: tmp_path / "mirror.git"
        )
        monkeypatch.setattr(runtime_setup_replay, "repair_mirror_hooks_path", _repair_os_error)
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())

        outcome = await _replay(factory, validation, ws_id, tmp_path, before=_OLD, after=_NEW)

        assert not outcome.ok
        assert outcome.details is not None
        assert outcome.details["cause_reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert validation.setup_runs() == 0

    @pytest.mark.unit
    @pytest.mark.parametrize("recovered", [True, False])
    async def test_cleanup_failure_repairs_hooks_then_recovers_missing_head(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        mirror_hooks: Any,
        recovered: bool,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        cleanup_error = ComposeExecCleanupError(
            invocation_id="inv", source="validation", label="setup", message="stuck"
        )
        validation = FakeToolValidation(FakeAgentContainer(), setup_error=cleanup_error)
        mirror_hooks(validation.trace)
        seen: list[ComposeExecCleanupError] = []

        async def _recover_missing_head(exc: ComposeExecCleanupError) -> bool:
            validation.trace.append("recover_head")
            seen.append(exc)
            return recovered

        outcome = await _replay(
            factory,
            validation,
            ws_id,
            tmp_path,
            before=_OLD,
            after=_NEW,
            recover_missing_head=_recover_missing_head,
        )

        assert validation.trace == [
            "repair_hooks:mirror.git",
            "phases:setup,pre_agent",
            "repair_hooks:mirror.git",
            "recover_head",
        ]
        assert seen == [cleanup_error]
        assert not outcome.ok
        assert outcome.details is not None
        assert outcome.details["cause_reason_code"] == EXEC_PROCESS_CLEANUP_FAILED
        assert outcome.details["head_recovered"] is recovered

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "callback_error",
        [
            OperationalError("SELECT 1", {}, Exception("database unavailable")),
            OSError("git spawn failed"),
        ],
    )
    async def test_cleanup_failure_head_recovery_error_still_fails_replay(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        mirror_hooks: Any,
        callback_error: Exception,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(
            FakeAgentContainer(),
            setup_error=ComposeExecCleanupError(
                invocation_id="inv", source="validation", label="setup", message="stuck"
            ),
        )
        mirror_hooks(validation.trace)

        async def _recover_missing_head(_exc: ComposeExecCleanupError) -> bool:
            raise callback_error

        outcome = await _replay(
            factory,
            validation,
            ws_id,
            tmp_path,
            before=_OLD,
            after=_NEW,
            recover_missing_head=_recover_missing_head,
        )

        assert not outcome.ok
        assert outcome.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert outcome.failure_reason == FailureReason.infrastructure_failure
        assert outcome.details is not None
        assert outcome.details["cause_reason_code"] == EXEC_PROCESS_CLEANUP_FAILED
        assert outcome.details["head_recovered"] is False
        assert type(callback_error).__name__ in outcome.details["head_recovery_error"]

    @pytest.mark.unit
    async def test_cleanup_failure_skips_head_recovery_when_hooks_repair_fails(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path, mirror_hooks: Any
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(
            FakeAgentContainer(),
            setup_error=ComposeExecCleanupError(
                invocation_id="inv", source="validation", label="setup", message="stuck"
            ),
        )
        mirror_hooks(validation.trace, fail_on_call=2)

        async def _recover_missing_head(_exc: ComposeExecCleanupError) -> bool:
            raise AssertionError("HEAD recovery must not run on a poisoned mirror")

        outcome = await _replay(
            factory,
            validation,
            ws_id,
            tmp_path,
            before=_OLD,
            after=_NEW,
            recover_missing_head=_recover_missing_head,
        )

        assert not outcome.ok
        assert outcome.details is not None
        assert outcome.details["cause_reason_code"] == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert outcome.details["mirror_hooks_repair_stage"] == "after_setup_replay_cleanup_failure"
        assert outcome.details["setup_cleanup_reason_code"] == EXEC_PROCESS_CLEANUP_FAILED


class _BrokenSession:
    async def __aenter__(self) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("database unavailable"))

    async def __aexit__(self, *_args: object) -> None:
        return None


class TestReplayAuditPersistenceFailures:
    @pytest.mark.unit
    async def test_db_errors_do_not_fail_a_successful_replay(self, tmp_path: Path) -> None:
        validation = FakeToolValidation(FakeAgentContainer())

        outcome = await _replay(
            lambda: _BrokenSession(),
            validation,
            "ws_db_down",
            tmp_path,
            before=_OLD,
            after=_NEW,
        )

        # The dedup lookup failed, so setup ran; the lost event is only logged.
        assert outcome.ok and outcome.replayed
        assert validation.setup_runs() == 1
