"""Monitor-handoff browser probe setup coverage.

Split from ``test_executor_error_paths_part_017.py`` to keep each first-party
test file under the maintainability line limit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from awf.control.executor import monitor_handoff_setup as monitor_handoff_setup_module
from awf.control.executor.monitor_handoff_setup import _run_monitor_handoff_profile_setup
from awf.profiles.models import WorkspaceProfile
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_017 import (
    _OkSetupValidation,
)


class _BrowserProbeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def invalidate(self) -> None:
        pass

    async def close(self) -> None:
        pass


class _CapturingSetupValidation(_OkSetupValidation):
    """Validation runner that records handoff-specific planner options."""

    def __init__(self, trace: list[str] | None = None) -> None:
        super().__init__(trace=trace)
        self.allow_browser_install_defer_flags: list[bool] = []

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        allow_browser_install_defer_to_unrequested_phase: bool = True,
        **kwargs: Any,
    ) -> Any:
        self.allow_browser_install_defer_flags.append(
            allow_browser_install_defer_to_unrequested_phase
        )
        return await super().run_profile_phases(phase_names=phase_names, **kwargs)


def _browser_probe_profile(*, validate_install: bool = False) -> WorkspaceProfile:
    phases: dict[str, list[str]] = {"setup": ["npm install"]}
    if validate_install:
        phases = {
            "setup": ["node scripts/generate-config.js"],
            "validate": ["pnpm install --frozen-lockfile", "pnpm test"],
        }
    return WorkspaceProfile.model_validate(
        {
            "name": "browser-handoff-test",
            "runtime": {"browsers": ["chromium"]},
            "phases": phases,
        }
    )


def _patch_browser_probe_session_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _scope(_session_factory: object) -> AsyncIterator[_BrowserProbeSession]:
        yield _BrowserProbeSession()

    monkeypatch.setattr(monitor_handoff_setup_module, "session_scope", _scope)


class TestHandoffSetupRunsBrowserProbe:
    @pytest.mark.unit
    async def test_setup_records_browser_findings_after_green_setup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_browser_probe_session_scope(monkeypatch)
        browser_calls: list[tuple[str, str]] = []
        trace: list[str] = []
        validation = _OkSetupValidation(trace=trace)

        class _Executor:
            _validation = validation
            _session_factory = object()

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                trace.append("record_setup_dependency_network_events")

            async def _record_runtime_toolchain_findings(self, **_kwargs: Any) -> None:
                trace.append("record_runtime_toolchain_findings")

            async def _record_runtime_browser_findings(
                self, *, workspace_id: str, compose_project: str, **_kwargs: Any
            ) -> None:
                trace.append("record_runtime_browser_findings")
                browser_calls.append((workspace_id, compose_project))

        ok = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-browser",
            profile=_browser_probe_profile(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert ok is True
        assert validation.calls == [("setup", "pre_agent")]
        assert browser_calls == [("ws-browser", "awf_x")]
        assert trace == [
            "run_profile_phases",
            "record_setup_dependency_network_events",
            "record_runtime_toolchain_findings",
            "record_runtime_browser_findings",
        ]

    @pytest.mark.unit
    async def test_setup_records_browser_findings_when_validate_install_is_unrequested(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_browser_probe_session_scope(monkeypatch)
        browser_calls: list[str] = []
        trace: list[str] = []
        validation = _CapturingSetupValidation(trace=trace)
        profile = _browser_probe_profile(validate_install=True)

        class _Executor:
            _validation = validation
            _session_factory = object()

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                trace.append("record_setup_dependency_network_events")

            async def _record_runtime_toolchain_findings(self, **_kwargs: Any) -> None:
                trace.append("record_runtime_toolchain_findings")

            async def _record_runtime_browser_findings(self, **_kwargs: Any) -> None:
                trace.append("record_runtime_browser_findings")
                browser_calls.append("called")

        ok = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-browser-deferred",
            profile=profile,
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert ok is True
        assert validation.calls == [("setup", "pre_agent")]
        assert validation.allow_browser_install_defer_flags == [False]
        assert browser_calls == ["called"]
        assert trace == [
            "run_profile_phases",
            "record_setup_dependency_network_events",
            "record_runtime_toolchain_findings",
            "record_runtime_browser_findings",
        ]

    @pytest.mark.unit
    async def test_setup_swallows_browser_recorder_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_browser_probe_session_scope(monkeypatch)
        log_calls: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def warning(self, event: str, **kwargs: Any) -> None:
                log_calls.append((event, kwargs))

        monkeypatch.setattr(monitor_handoff_setup_module, "_log", _Logger())

        trace: list[str] = []
        validation = _OkSetupValidation(trace=trace)

        class _RecorderError(RuntimeError):
            reason_code = "BROWSER_RECORDER_BROKE"

        class _Executor:
            _validation = validation
            _session_factory = object()

            async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
                trace.append("record_setup_dependency_network_events")

            async def _record_runtime_toolchain_findings(self, **_kwargs: Any) -> None:
                trace.append("record_runtime_toolchain_findings")

            async def _record_runtime_browser_findings(self, **_kwargs: Any) -> None:
                trace.append("record_runtime_browser_findings")
                raise _RecorderError("recorder unavailable ghp_FAKESECRET0000000")

        ok = await _run_monitor_handoff_profile_setup(
            _Executor(),
            workspace_id="ws-browser-boom",
            profile=_browser_probe_profile(),
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
        )

        assert ok is True
        assert trace == [
            "run_profile_phases",
            "record_setup_dependency_network_events",
            "record_runtime_toolchain_findings",
            "record_runtime_browser_findings",
        ]
        assert [event for event, _ in log_calls] == [
            "executor.monitor_handoff_runtime_browser_probe_record_failed"
        ]
        _, kwargs = log_calls[0]
        assert kwargs["reason_code"] == "BROWSER_RECORDER_BROKE"
        assert "ghp_FAKESECRET0000000" not in kwargs["error"]
        assert "<redacted>" in kwargs["error"]
