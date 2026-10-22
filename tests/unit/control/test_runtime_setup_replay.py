"""Core runtime setup replay: identity capture, recreation decision, replay (#787, #1006)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.control.executor import runtime_setup_replay
from awf.control.executor.constants import (
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
    RUNTIME_SETUP_REPLAYED_REASON_CODE,
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
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
)
from awf.runtime.validation import (
    SETUP_DEPENDENCY_NETWORK_FAILURE,
    SETUP_DEPENDENCY_NETWORK_METADATA_KEY,
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
) -> runtime_setup_replay.SetupReplayOutcome:
    return await replay_setup_if_recreated(
        SimpleNamespace(),
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
                RecreationDecision.recreated,
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
    async def test_unknown_identity_with_healthy_env_does_not_replay(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        container = FakeAgentContainer()
        container.tmp.add(TOOL_PATH)
        validation = FakeToolValidation(container)

        outcome = await _replay(
            factory, validation, ws_id, tmp_path, before=_OLD, after=IDENTITY_UNAVAILABLE
        )

        assert outcome.ok and not outcome.replayed
        assert validation.probe_calls == 1
        assert validation.phase_calls == []

    @pytest.mark.unit
    async def test_unknown_identity_with_missing_tool_replays(
        self, factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        validation = FakeToolValidation(FakeAgentContainer())

        outcome = await _replay(
            factory, validation, ws_id, tmp_path, before=_OLD, after=IDENTITY_UNAVAILABLE
        )

        assert outcome.ok and outcome.replayed
        assert validation.phase_calls == [("setup", "pre_agent")]
        events = await _replay_events(factory, ws_id)
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["basis"] == "identity_unavailable"
        assert events[0].payload["container_id"] is None

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
