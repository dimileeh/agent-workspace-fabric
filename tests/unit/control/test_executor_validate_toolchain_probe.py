"""Executor wiring for the adopt-pr ``validate``-toolchain provisioning probe.

Issue #574: a profile whose ``validate`` phase runs a tool its ``setup`` phase
never installs works on the create path (the coding agent's startup installs the
repo deps as a side effect) but kills the adopt-pr path, where
``PR_ADOPTION_SKIP_AGENT`` skips the agent and the first validate command dies
``127`` later at ``sync_base_push``. After a green adopt-pr handoff setup, AWF
must probe each validate command's executable and fail the adoption early and
clearly with ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED`` instead of reaching
``monitoring_pr``.

These cases drive the real ``_run_monitor_handoff_profile_setup`` with a
validation double exposing ``probe_validate_command_tools`` and assert the
early-fail wiring: missing -> failed with the new reason code (the RED
reproduction; passes only with the production wiring), present/empty -> proceed,
probe-infra error -> proceed (never mis-reported as a profile gap), legacy stub
-> proceed, multiple missing -> all named.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.control.executor import monitor_handoff_setup as monitor_handoff_setup_module
from awf.control.executor.constants import (
    PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED_REASON_CODE,
)
from awf.control.executor.monitor_handoff_setup import _run_monitor_handoff_profile_setup
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.runtime.validation import (
    ValidateCommandProbeTarget,
    ValidateToolProbeResult,
    ValidationResult,
)


class _OkSetupValidation:
    """Validation runner whose setup/pre_agent phase always passes.

    ``probe_validate_command_tools`` returns a canned ``ValidateToolProbeResult``
    (or raises ``probe_error`` to model a probe-infra failure).
    """

    def __init__(
        self,
        *,
        probe_result: ValidateToolProbeResult | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.probe_calls: list[str] = []
        self._probe_result = probe_result
        self._probe_error = probe_error

    async def run_profile_phases(
        self,
        *,
        phase_names: tuple[str, ...],
        **_kwargs: Any,
    ) -> ValidationResult:
        self.calls.append(phase_names)
        return ValidationResult()

    async def probe_validate_command_tools(
        self,
        *,
        workspace_id: str,
        **_kwargs: Any,
    ) -> ValidateToolProbeResult:
        self.probe_calls.append(workspace_id)
        if self._probe_error is not None:
            raise self._probe_error
        assert self._probe_result is not None
        return self._probe_result


class _RecordingExecutor:
    """Minimal executor exposing the collaborators the setup path touches.

    ``_mark_failed`` records its kwargs and transitions a tracked in-memory
    status so a test can assert the workspace never reached ``monitoring_pr``.
    """

    def __init__(self, validation: Any) -> None:
        self._validation = validation
        self.status = WorkspaceStatus.running
        self.mark_failed_calls: list[dict[str, Any]] = []

    async def _record_setup_dependency_network_events(self, **_kwargs: Any) -> None:
        return None

    async def _record_runtime_toolchain_findings(self, **_kwargs: Any) -> None:
        return None

    async def _mark_failed(self, **kwargs: Any) -> None:
        self.mark_failed_calls.append(kwargs)
        self.status = WorkspaceStatus.failed


@pytest.fixture(autouse=True)
def _allow_runtime_ownership_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _repair(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        monitor_handoff_setup_module,
        "repair_agent_runtime_ownership",
        _repair,
        raising=False,
    )


async def _run_setup(executor: Any, tmp_path: Path) -> bool:
    return await _run_monitor_handoff_profile_setup(
        executor,
        workspace_id="ws-574",
        profile=object(),
        compose_project="awf_x",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path,
    )


class TestHandoffValidateToolchainProbe:
    @pytest.mark.unit
    async def test_missing_validate_tool_fails_early_with_new_reason_code(
        self,
        tmp_path: Path,
    ) -> None:
        # RED reproduction: a profile whose validate needs ``ruff`` that is not on
        # PATH after setup must fail the adoption here with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED, naming the tool + setup, and
        # never proceed to the preflight / monitoring_pr. Without the production
        # wiring the probe is never called, setup returns True, and this fails.
        validation = _OkSetupValidation(
            probe_result=ValidateToolProbeResult(
                missing=(ValidateCommandProbeTarget(tool="ruff", command="ruff check ."),),
            )
        )
        executor = _RecordingExecutor(validation)

        ok = await _run_setup(executor, tmp_path)

        assert ok is False
        assert validation.probe_calls == ["ws-574"]
        assert executor.status == WorkspaceStatus.failed
        assert len(executor.mark_failed_calls) == 1
        call = executor.mark_failed_calls[0]
        assert call["reason_code"] == PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED_REASON_CODE
        assert call["failure_reason"] == FailureReason.infrastructure_failure
        assert call["from_status"] == WorkspaceStatus.running
        assert "ruff" in call["message"]
        assert "`ruff check .`" in call["message"]
        assert "setup" in call["message"]
        assert "Missing tools: ruff." in call["message"]
        assert call["details"] == {"missing_tools": ["ruff"]}

    @pytest.mark.unit
    async def test_all_validate_tools_present_proceeds(
        self,
        tmp_path: Path,
    ) -> None:
        # No-regression: when every validate tool resolves, the probe reports
        # nothing missing and the handoff proceeds (setup returns True, no failure).
        validation = _OkSetupValidation(probe_result=ValidateToolProbeResult())
        executor = _RecordingExecutor(validation)

        ok = await _run_setup(executor, tmp_path)

        assert ok is True
        assert validation.probe_calls == ["ws-574"]
        assert executor.mark_failed_calls == []
        assert executor.status == WorkspaceStatus.running

    @pytest.mark.unit
    async def test_probe_infra_error_proceeds_without_failing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A ``probe_errored`` result is a probe-infra failure, not a profile gap:
        # the handoff proceeds and a redacted warning is logged rather than
        # marking the workspace UNPROVISIONED.
        log_calls: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def warning(self, event: str, **kwargs: Any) -> None:
                log_calls.append((event, kwargs))

        monkeypatch.setattr(monitor_handoff_setup_module, "_log", _Logger())

        validation = _OkSetupValidation(probe_result=ValidateToolProbeResult(probe_errored=True))
        executor = _RecordingExecutor(validation)

        ok = await _run_setup(executor, tmp_path)

        assert ok is True
        assert executor.mark_failed_calls == []
        assert [event for event, _ in log_calls] == [
            "executor.monitor_handoff_validate_toolchain_probe_errored"
        ]

    @pytest.mark.unit
    async def test_probe_raising_is_non_blocking_and_redacted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A probe that raises (e.g. compose-exec defect) must never fail the
        # adoption: the error is logged with its structured reason_code and
        # redacted, and the handoff proceeds.
        log_calls: list[tuple[str, dict[str, Any]]] = []

        class _Logger:
            def warning(self, event: str, **kwargs: Any) -> None:
                log_calls.append((event, kwargs))

        monkeypatch.setattr(monitor_handoff_setup_module, "_log", _Logger())

        class _ProbeError(RuntimeError):
            reason_code = "VALIDATE_PROBE_BROKE"

        validation = _OkSetupValidation(
            probe_error=_ProbeError("probe blew up ghp_FAKESECRET0000000")
        )
        executor = _RecordingExecutor(validation)

        ok = await _run_setup(executor, tmp_path)

        assert ok is True
        assert executor.mark_failed_calls == []
        assert [event for event, _ in log_calls] == [
            "executor.monitor_handoff_validate_toolchain_probe_failed"
        ]
        _, kwargs = log_calls[0]
        assert kwargs["reason_code"] == "VALIDATE_PROBE_BROKE"
        assert "ghp_FAKESECRET0000000" not in kwargs["error"]
        assert "<redacted>" in kwargs["error"]

    @pytest.mark.unit
    async def test_legacy_validation_without_probe_proceeds(
        self,
        tmp_path: Path,
    ) -> None:
        # A validation runner predating the probe (no
        # ``probe_validate_command_tools`` attribute) is a no-op: the ``getattr``
        # guard returns True and the handoff proceeds, exactly like the existing
        # toolchain recorder's legacy-stub handling.
        class _LegacyValidation:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            async def run_profile_phases(
                self, *, phase_names: tuple[str, ...], **_kwargs: Any
            ) -> ValidationResult:
                self.calls.append(phase_names)
                return ValidationResult()

        validation = _LegacyValidation()
        executor = _RecordingExecutor(validation)

        ok = await _run_setup(executor, tmp_path)

        assert ok is True
        assert executor.mark_failed_calls == []
        assert executor.status == WorkspaceStatus.running

    @pytest.mark.unit
    async def test_multiple_missing_tools_all_reported(
        self,
        tmp_path: Path,
    ) -> None:
        # Every missing tool is named in the failure message and details, sorted
        # and deduped, with the first missing command kept as the representative.
        validation = _OkSetupValidation(
            probe_result=ValidateToolProbeResult(
                missing=(
                    ValidateCommandProbeTarget(tool="ruff", command="ruff check ."),
                    ValidateCommandProbeTarget(tool="mypy", command="mypy src"),
                ),
            )
        )
        executor = _RecordingExecutor(validation)

        ok = await _run_setup(executor, tmp_path)

        assert ok is False
        call = executor.mark_failed_calls[0]
        assert call["details"] == {"missing_tools": ["mypy", "ruff"]}
        assert "Missing tools: mypy, ruff." in call["message"]
        # The representative command (first missing) anchors the headline.
        assert "`ruff check .`" in call["message"]
