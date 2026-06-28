"""Executor wiring for provision-time Playwright browser availability warnings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.executor.constants import RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
from awf.control.executor.monitor_handoff_audit import (
    _record_runtime_browser_findings,
    _record_runtime_browser_findings_safe,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import session_scope
from awf.profiles.models import (
    RUNTIME_BROWSER_UNAVAILABLE,
    ProfileLintFinding,
    ProfileLintSeverity,
)
from tests.unit.control.test_executor_parts.test_executor_part_005 import (
    _seed_ready_workspace,
    factory,
)

_IMPORTED_FIXTURES = (factory,)


def _browser_finding(browser: str) -> ProfileLintFinding:
    return ProfileLintFinding(
        reason_code=RUNTIME_BROWSER_UNAVAILABLE,
        message=f"runtime does not provide Playwright browser {browser}",
        path="runtime.browsers",
        severity=ProfileLintSeverity.warning,
        details={"browser": browser, "available_browsers": ["firefox"]},
    )


class _FindingsValidation:
    def __init__(self, findings: tuple[ProfileLintFinding, ...]) -> None:
        self._findings = findings
        self.calls: list[str] = []

    async def probe_runtime_browser_findings(
        self, *, workspace_id: str, **_kwargs: Any
    ) -> tuple[ProfileLintFinding, ...]:
        self.calls.append(workspace_id)
        return self._findings


class TestRecordRuntimeBrowserFindings:
    @pytest.mark.unit
    async def test_records_with_caller_owned_session_boundary(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        validation = _FindingsValidation((_browser_finding("chromium"),))

        class _Executor:
            _validation = validation

        async with session_scope(factory) as session:
            await _record_runtime_browser_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=session,
            )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            events = [
                e for e in ws.events if e.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
            ]
            assert len(events) == 1
            assert events[0].reason_code == RUNTIME_BROWSER_UNAVAILABLE

    @pytest.mark.unit
    async def test_emits_one_warning_event_per_finding(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        validation = _FindingsValidation((_browser_finding("chromium"),))

        class _Executor:
            _session_factory = factory
            _validation = validation

        async with session_scope(factory) as session:
            await _record_runtime_browser_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=session,
            )

        assert validation.calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            events = [
                e for e in ws.events if e.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
            ]
            assert len(events) == 1
            assert events[0].reason_code == RUNTIME_BROWSER_UNAVAILABLE
            assert events[0].payload == {
                "browser": "chromium",
                "available_browsers": ["firefox"],
                "path": "runtime.browsers",
                "message": "runtime does not provide Playwright browser chromium",
            }

    @pytest.mark.unit
    async def test_repeated_missing_browser_finding_records_single_event(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        validation = _FindingsValidation((_browser_finding("chromium"),))

        class _Executor:
            _session_factory = factory
            _validation = validation

        async with session_scope(factory) as session:
            await _record_runtime_browser_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=session,
            )
            await _record_runtime_browser_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=session,
            )

        assert validation.calls == [ws_id, ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            events = [
                e for e in ws.events if e.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
            ]
            assert len(events) == 1
            assert events[0].payload["browser"] == "chromium"

    @pytest.mark.unit
    async def test_legacy_validation_without_probe_is_noop(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        class _LegacyValidation:
            pass

        class _Executor:
            _session_factory = factory
            _validation = _LegacyValidation()

        async with session_scope(factory) as session:
            await _record_runtime_browser_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=session,
            )

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert [
                e for e in ws.events if e.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
            ] == []

    @pytest.mark.unit
    async def test_expected_probe_infra_exception_is_swallowed(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        class _ProbeError(OSError):
            reason_code = "RUNTIME_BROWSER_PROBE_BROKE"

        class _ExplodingValidation:
            async def probe_runtime_browser_findings(self, **_kwargs: Any) -> Any:
                raise _ProbeError("probe blew up ghp_FAKESECRET0000000")

        class _Executor:
            _session_factory = factory
            _validation = _ExplodingValidation()

        with structlog.testing.capture_logs() as captured:
            async with session_scope(factory) as session:
                await _record_runtime_browser_findings(
                    _Executor(),
                    workspace_id=ws_id,
                    compose_project="awf_x",
                    compose_file=Path("/tmp/compose.yml"),
                    profile=object(),
                    session=session,
                )

        entry = next(e for e in captured if e["event"] == "executor.runtime_browser_probe_failed")
        assert entry["log_level"] == "warning"
        assert entry["reason_code"] == "RUNTIME_BROWSER_PROBE_BROKE"
        assert "ghp_FAKESECRET0000000" not in entry["error"]
        assert "<redacted>" in entry["error"]

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            assert [
                e for e in ws.events if e.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
            ] == []

    @pytest.mark.unit
    async def test_unexpected_probe_exception_propagates_to_safe_wrapper(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        class _ExplodingValidation:
            async def probe_runtime_browser_findings(self, **_kwargs: Any) -> Any:
                raise RuntimeError("probe regression ghp_FAKESECRET0000000")

        class _Executor:
            _session_factory = factory
            _validation = _ExplodingValidation()
            _record_runtime_browser_findings = _record_runtime_browser_findings

        with pytest.raises(RuntimeError, match="probe regression"):
            async with session_scope(factory) as session:
                await _record_runtime_browser_findings(
                    _Executor(),
                    workspace_id=ws_id,
                    compose_project="awf_x",
                    compose_file=Path("/tmp/compose.yml"),
                    profile=object(),
                    session=session,
                )

        with structlog.testing.capture_logs() as captured:
            await _record_runtime_browser_findings_safe(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
            )

        assert not [e for e in captured if e["event"] == "executor.runtime_browser_probe_failed"]
        entry = next(
            e for e in captured if e["event"] == "executor.runtime_browser_probe_record_failed"
        )
        assert entry["log_level"] == "warning"
        assert entry["reason_code"] is None
        assert "ghp_FAKESECRET0000000" not in entry["error"]
        assert "<redacted>" in entry["error"]

    @pytest.mark.unit
    async def test_no_findings_are_noop(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        validation = _FindingsValidation(())

        class _Executor:
            _session_factory = factory
            _validation = validation

        async with session_scope(factory) as session:
            await _record_runtime_browser_findings(
                _Executor(),
                workspace_id=ws_id,
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=session,
            )

        assert validation.calls == [ws_id]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert [
                e for e in ws.events if e.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
            ] == []

    @pytest.mark.unit
    async def test_safe_wrapper_swallows_recorder_exception(self) -> None:
        class _RecorderError(RuntimeError):
            reason_code = "RUNTIME_BROWSER_RECORD_BROKE"

        class _Executor:
            async def _record_runtime_browser_findings(self, **_kwargs: Any) -> None:
                raise _RecorderError("record failed ghp_FAKESECRET0000000")

        with structlog.testing.capture_logs() as captured:
            await _record_runtime_browser_findings_safe(
                _Executor(),
                workspace_id="ws-1",
                compose_project="awf_x",
                compose_file=Path("/tmp/compose.yml"),
                profile=object(),
                session=cast(AsyncSession, object()),
            )

        entry = next(
            e for e in captured if e["event"] == "executor.runtime_browser_probe_record_failed"
        )
        assert entry["log_level"] == "warning"
        assert entry["reason_code"] == "RUNTIME_BROWSER_RECORD_BROKE"
        assert "ghp_FAKESECRET0000000" not in entry["error"]
        assert "<redacted>" in entry["error"]
