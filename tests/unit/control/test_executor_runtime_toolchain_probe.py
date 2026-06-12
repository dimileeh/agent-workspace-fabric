"""Executor wiring for the provision-time toolchain-availability probe.

Covers ``_record_runtime_toolchain_findings`` (emit one warning event per
finding; legacy-stub and best-effort no-ops) and the non-blocking guarded call
site in ``execution_flow.execute`` (probe findings never fail provisioning; a
recorder error is swallowed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor.constants import RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE
from awf.control.executor.monitor_handoff_audit import _record_runtime_toolchain_findings
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeManager
from awf.profiles.models import (
    RUNTIME_TOOLCHAIN_UNAVAILABLE,
    ProfileLintFinding,
    ProfileLintSeverity,
)
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.unit.control.test_executor_parts.test_executor_part_005 import (
    _TEMPLATE,
    _queue_pre_push_diagnostics,
    _queue_validation_head,
    _seed_ready_workspace,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


def _java_finding(version: str) -> ProfileLintFinding:
    return ProfileLintFinding(
        reason_code=RUNTIME_TOOLCHAIN_UNAVAILABLE,
        message=f"runtime image does not provide java toolchain version {version}",
        path="runtime.toolchains.java",
        severity=ProfileLintSeverity.warning,
        details={"language": "java", "version": version, "available_versions": ["17"]},
    )


class _FindingsValidation:
    """Validation double whose probe returns canned toolchain findings."""

    def __init__(self, findings: tuple[ProfileLintFinding, ...]) -> None:
        self._findings = findings
        self.calls: list[str] = []

    async def probe_runtime_toolchain_findings(
        self, *, workspace_id: str, **_kwargs: Any
    ) -> tuple[ProfileLintFinding, ...]:
        self.calls.append(workspace_id)
        return self._findings


class TestRecordRuntimeToolchainFindings:
    @pytest.mark.unit
    async def test_emits_one_warning_event_per_finding(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        validation = _FindingsValidation((_java_finding("21"), _java_finding("23")))

        class _Executor:
            _session_factory = factory
            _validation = validation

        await _record_runtime_toolchain_findings(
            _Executor(),
            workspace_id=ws_id,
            compose_project="awf_x",
            compose_file=Path("/tmp/compose.yml"),
            profile=object(),
        )

        assert validation.calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # The probe is non-blocking: status is unchanged.
            assert ws.status == WorkspaceStatus.ready.value
            # ``ws.events`` is ordered only by ``occurred_at``; events written
            # together can share a timestamp, so sort on the monotonic
            # ``event_order`` (the canonical insertion tiebreak) for a
            # backend-independent, deterministic assertion on emission order.
            events = sorted(
                (e for e in ws.events if e.event_type == RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE),
                key=lambda e: e.event_order,
            )
            assert [e.payload["version"] for e in events] == ["21", "23"]
            assert all(e.reason_code == RUNTIME_TOOLCHAIN_UNAVAILABLE for e in events)
            assert events[0].payload["language"] == "java"
            assert events[0].payload["available_versions"] == ["17"]
            assert events[0].payload["path"] == "runtime.toolchains.java"

    @pytest.mark.unit
    async def test_no_findings_records_nothing(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        class _Executor:
            _session_factory = factory
            _validation = _FindingsValidation(())

        await _record_runtime_toolchain_findings(
            _Executor(),
            workspace_id=ws_id,
            compose_project="awf_x",
            compose_file=Path("/tmp/compose.yml"),
            profile=object(),
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert [
                e for e in ws.events if e.event_type == RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE
            ] == []

    @pytest.mark.unit
    async def test_legacy_validation_without_probe_is_noop(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        class _LegacyValidation:
            """No ``probe_runtime_toolchain_findings`` attribute at all."""

        class _Executor:
            _session_factory = factory
            _validation = _LegacyValidation()

        await _record_runtime_toolchain_findings(
            _Executor(),
            workspace_id=ws_id,
            compose_project="awf_x",
            compose_file=Path("/tmp/compose.yml"),
            profile=object(),
        )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_probe_exception_is_swallowed(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        class _ProbeError(RuntimeError):
            reason_code = "RUNTIME_TOOLCHAIN_PROBE_BROKE"

        class _ExplodingValidation:
            async def probe_runtime_toolchain_findings(self, **_kwargs: Any) -> Any:
                raise _ProbeError("probe blew up ghp_FAKESECRET0000000")

        class _Executor:
            _session_factory = factory
            _validation = _ExplodingValidation()

        # Must not raise.
        with structlog.testing.capture_logs() as captured:
            await _record_runtime_toolchain_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
            )

        # The swallowed failure is the only operator signal: it must preserve the
        # structured reason_code and never leak the compose-exec stderr secret as
        # a raw traceback.
        entry = next(e for e in captured if e["event"] == "executor.runtime_toolchain_probe_failed")
        assert entry["log_level"] == "warning"
        assert entry["reason_code"] == "RUNTIME_TOOLCHAIN_PROBE_BROKE"
        assert "ghp_FAKESECRET0000000" not in entry["error"]
        assert "<redacted>" in entry["error"]
        assert "exc_info" not in entry

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert [
                e for e in ws.events if e.event_type == RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE
            ] == []


def _make_handoff_executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monitor: Any,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
        pr_monitor=monitor,
    )


class _CompletingMonitor:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.calls: list[str] = []

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        del compose_project, compose_file
        self.calls.append(workspace_id)
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            await WorkspaceRepository(s).transition(
                ws, to=WorkspaceStatus.completed, reason_code="STUB_MERGE"
            )
            await s.commit()


def _queue_happy_execute(fake: FakeCommandRunner, ws_id: str) -> None:
    fake.queue_result(returncode=0)  # adapter
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add
    fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
    _queue_validation_head(fake)
    fake.queue_result(returncode=0)  # validation cmd
    _queue_pre_push_diagnostics(fake)
    fake.queue_result(returncode=0)  # push
    fake.queue_result(
        returncode=0,
        stdout="https://github.com/dimileeh/aira-web/pull/7777\n",
    )


class TestExecuteGuardedProbeCall:
    @pytest.mark.unit
    async def test_probe_findings_recorded_without_blocking_provisioning(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A reachable image missing a declared toolchain version surfaces a
        # warning event mid-execute, yet provisioning proceeds to completion.
        monitor = _CompletingMonitor(factory)
        executor = _make_handoff_executor(fake, factory, tmp_path, monitor)

        async def _probe(**_kwargs: Any) -> tuple[ProfileLintFinding, ...]:
            return (_java_finding("21"),)

        monkeypatch.setattr(executor._validation, "probe_runtime_toolchain_findings", _probe)

        ws_id = await _seed_ready_workspace(
            factory,
            compose_file_path=str(tmp_path / "rendered" / "compose.yml"),
        )
        _queue_happy_execute(fake, ws_id)

        await executor.execute(ws_id)

        assert monitor.calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            toolchain_events = [
                e for e in ws.events if e.event_type == RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE
            ]
            assert len(toolchain_events) == 1
            assert toolchain_events[0].payload["version"] == "21"
            assert toolchain_events[0].reason_code == RUNTIME_TOOLCHAIN_UNAVAILABLE

    @pytest.mark.unit
    async def test_recorder_failure_is_swallowed_and_provisioning_proceeds(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The call site double-guards: even if the recorder itself raises, the
        # workspace still hands off to the monitor and completes.
        monitor = _CompletingMonitor(factory)
        executor = _make_handoff_executor(fake, factory, tmp_path, monitor)

        async def _boom(**_kwargs: Any) -> None:
            raise RuntimeError("recorder unavailable")

        monkeypatch.setattr(executor, "_record_runtime_toolchain_findings", _boom)

        ws_id = await _seed_ready_workspace(
            factory,
            compose_file_path=str(tmp_path / "rendered" / "compose.yml"),
        )
        _queue_happy_execute(fake, ws_id)

        await executor.execute(ws_id)

        assert monitor.calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
