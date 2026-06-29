"""Additional edge coverage for PR monitor pre-push validation helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED, ComposeExecCleanupError
from awf.control.executor.constants import RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import (
    RUNTIME_BROWSER_UNAVAILABLE,
    ProfileLintFinding,
    ProfileLintSeverity,
    WorkspaceProfile,
)
from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
from awf.runtime.validation_types import ValidationCoverageResult, ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _command_result,
    _FakeValidation,
    _mark_git_worktree,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _existing_mirror_commit(
    self: object,
    mirror_path: Path,
    commit_sha: str,
) -> bool:
    del self, mirror_path, commit_sha
    return True


def _deferred_browser_profile() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "browser-pre-push-helper-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": ["pnpm install --frozen-lockfile", "pnpm test:e2e"],
            },
        }
    )


@pytest.mark.unit
def test_deferred_browser_install_completed_after_green_pre_push_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A green pre-push result is ready for the deferred browser probe."""
    monkeypatch.setattr(
        pre_push_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm install --frozen-lockfile"),
            ),
            SimpleNamespace(
                phase="setup",
                command=SimpleNamespace(command="pnpm exec playwright install chromium"),
            ),
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm test:e2e"),
            ),
        ],
    )

    assert pre_push_validation._deferred_runtime_browser_install_completed(
        _deferred_browser_profile(),
        worktree_path=tmp_path,
        result=ValidationResult(
            commands=[
                _command_result(
                    tmp_path,
                    ok=True,
                    command="pnpm install --frozen-lockfile",
                    artifact_name="pnpm_install",
                ),
                _command_result(
                    tmp_path,
                    ok=True,
                    command="pnpm test:e2e",
                    artifact_name="pnpm_test_e2e",
                ),
            ],
        ),
    )


@pytest.mark.unit
def test_deferred_browser_install_completed_for_validate_runtime_browser_install_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate-phase browser install injections are install evidence."""
    monkeypatch.setattr(
        pre_push_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(
                    command="source .env && pnpm install",
                    runtime_browser_install=True,
                ),
            ),
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm exec playwright test"),
            ),
        ],
    )

    assert pre_push_validation._deferred_runtime_browser_install_completed(
        _deferred_browser_profile(),
        worktree_path=tmp_path,
        result=ValidationResult(
            commands=[
                _command_result(
                    tmp_path,
                    ok=True,
                    command="source .env && pnpm install",
                    artifact_name="pnpm_install",
                ),
                _command_result(
                    tmp_path,
                    ok=True,
                    command="pnpm exec playwright test",
                    artifact_name="playwright_test",
                ),
            ],
        ),
    )


@pytest.mark.unit
def test_deferred_browser_install_completed_after_later_blocking_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking command after the deferred install means the probe can run."""
    monkeypatch.setattr(
        pre_push_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm install --frozen-lockfile"),
            ),
            SimpleNamespace(
                phase="setup",
                command=SimpleNamespace(command="pnpm exec playwright install chromium"),
            ),
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm test:e2e"),
            ),
        ],
    )

    assert pre_push_validation._deferred_runtime_browser_install_completed(
        _deferred_browser_profile(),
        worktree_path=tmp_path,
        result=ValidationResult(
            commands=[
                _command_result(
                    tmp_path,
                    ok=False,
                    command="pnpm test:e2e",
                    reason_code="VALIDATION_COMMAND_FAILED",
                    artifact_name="pnpm_test_e2e_failed",
                ),
            ],
        ),
    )


@pytest.mark.unit
def test_deferred_browser_install_not_completed_for_early_duplicate_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate command text before the install must not appear post-install."""
    monkeypatch.setattr(
        pre_push_validation,
        "profile_phase_command_plan",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm install --frozen-lockfile"),
            ),
            SimpleNamespace(
                phase="setup",
                command=SimpleNamespace(command="pnpm exec playwright install chromium"),
            ),
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm test:e2e"),
            ),
            SimpleNamespace(
                phase="validate",
                command=SimpleNamespace(command="pnpm install --frozen-lockfile"),
            ),
        ],
    )

    assert not pre_push_validation._deferred_runtime_browser_install_completed(
        _deferred_browser_profile(),
        worktree_path=tmp_path,
        result=ValidationResult(
            commands=[
                _command_result(
                    tmp_path,
                    ok=False,
                    command="pnpm install --frozen-lockfile",
                    reason_code="VALIDATION_COMMAND_FAILED",
                    artifact_name="pnpm_install_failed",
                ),
            ],
        ),
    )


@pytest.mark.unit
async def test_pre_push_validation_skips_deferred_browser_probe_on_infra_failure_before_install(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Infrastructure failures must not report browser gaps before install evidence."""
    workspace_id = await seed_monitoring_workspace(factory)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-pre-push-infra-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": ["pnpm install --frozen-lockfile", "pnpm test:e2e"],
            },
        }
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = profile.model_dump(mode="json", by_alias=True)
        await session.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")

    async def _cleanup(
        _self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del _self, worktree_path
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    class _BrowserValidation(_FakeValidation):
        def __init__(self) -> None:
            super().__init__(RuntimeError("pre-push validation infrastructure unavailable"))
            self.probe_calls: list[dict[str, object]] = []

        async def probe_runtime_browser_findings(
            self, **kwargs: object
        ) -> tuple[ProfileLintFinding, ...]:
            self.probe_calls.append(dict(kwargs))
            return (
                ProfileLintFinding(
                    reason_code=RUNTIME_BROWSER_UNAVAILABLE,
                    message="runtime does not provide Playwright browser chromium",
                    path="runtime.browsers",
                    severity=ProfileLintSeverity.warning,
                    details={"browser": "chromium", "available_browsers": []},
                ),
            )

    validation = _BrowserValidation()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(pre_push_validation, "_pre_push_validation_cleanup", _cleanup)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert validation.probe_calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        browser_events = [
            event
            for event in ws.events
            if event.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
        ]
    assert browser_events == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("coverage_error", "expected_run_reason"),
    [
        (
            ComposeExecCleanupError(
                invocation_id="awf_pre_push_coverage_cleanup",
                source="validation",
                label="coverage",
                message="coverage process still running",
            ),
            EXEC_PROCESS_CLEANUP_FAILED,
        ),
        (
            RuntimeError("coverage provider crashed"),
            "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED",
        ),
    ],
)
async def test_pre_push_validation_records_deferred_browser_findings_after_coverage_exception(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coverage_error: Exception,
    expected_run_reason: str,
) -> None:
    """Coverage infrastructure failures must not drop completed browser probe warnings."""
    workspace_id = await seed_monitoring_workspace(factory)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-pre-push-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "setup": ["node scripts/generate-config.js"],
                "validate": ["pnpm install --frozen-lockfile", "pnpm test:e2e"],
            },
            "validation": {
                "coverage": {
                    "minimum_percent": 99.0,
                    "command": "coverage run -m pytest && coverage report",
                },
                "strategy": {"final_gate": "coverage"},
            },
        }
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = profile.model_dump(mode="json", by_alias=True)
        await session.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    order: list[str] = []

    async def _cleanup(
        _self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del _self, worktree_path
        order.append("cleanup")
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    class _CoverageExceptionBrowserValidation(_FakeValidation):
        def __init__(self, exc: Exception) -> None:
            super().__init__(
                ValidationResult(
                    commands=[
                        _command_result(
                            tmp_path,
                            ok=True,
                            command="pnpm install --frozen-lockfile",
                            artifact_name="pnpm_install",
                        ),
                        _command_result(
                            tmp_path,
                            ok=True,
                            command="pnpm exec playwright install chromium",
                            artifact_name="playwright_install",
                        ),
                        _command_result(
                            tmp_path,
                            ok=True,
                            command="pnpm test:e2e",
                            artifact_name="pnpm_test_e2e",
                        ),
                    ]
                )
            )
            self.exc = exc
            self.probe_calls: list[dict[str, object]] = []

        async def run_profile_coverage(self, **kwargs: object) -> ValidationCoverageResult | None:
            self.coverage_calls.append(dict(kwargs))
            raise self.exc

        async def probe_runtime_browser_findings(
            self, **kwargs: object
        ) -> tuple[ProfileLintFinding, ...]:
            order.append("browser_probe")
            self.probe_calls.append(dict(kwargs))
            return (
                ProfileLintFinding(
                    reason_code=RUNTIME_BROWSER_UNAVAILABLE,
                    message="runtime does not provide Playwright browser chromium",
                    path="runtime.browsers",
                    severity=ProfileLintSeverity.warning,
                    details={"browser": "chromium", "available_browsers": ["firefox"]},
                ),
            )

    validation = _CoverageExceptionBrowserValidation(coverage_error)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(pre_push_validation, "_pre_push_validation_cleanup", _cleanup)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert validation.coverage_calls == [
        {
            "workspace_id": workspace_id,
            "compose_project": "proj",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "phase": "coverage",
        }
    ]
    assert validation.probe_calls == [
        {
            "workspace_id": workspace_id,
            "compose_project": "proj",
            "compose_file": tmp_path / "compose.yml",
            "profile": profile,
            "worktree_path": worktree,
        }
    ]
    assert order == ["browser_probe", "cleanup"]
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        browser_events = [
            event
            for event in ws.events
            if event.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
        ]
    assert len(browser_events) == 1
    assert browser_events[0].reason_code == RUNTIME_BROWSER_UNAVAILABLE
    assert browser_events[0].payload == {
        "browser": "chromium",
        "available_browsers": ["firefox"],
        "path": "runtime.browsers",
        "message": "runtime does not provide Playwright browser chromium",
    }
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "failed"
    assert runs[-1].reason_code == expected_run_reason


@pytest.mark.unit
async def test_pre_push_validation_skips_deferred_browser_probe_before_install_runs(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-install validation failure must not be reported as a browser gap."""
    workspace_id = await seed_monitoring_workspace(factory)
    profile = WorkspaceProfile.model_validate(
        {
            "name": "browser-pre-push-early-failure-profile",
            "runtime": {"browsers": ["chromium"]},
            "phases": {
                "post_agent": ["exit 1"],
                "validate": ["pnpm install --frozen-lockfile", "pnpm test:e2e"],
            },
        }
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = profile.model_dump(mode="json", by_alias=True)
        await session.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")

    async def _cleanup(
        _self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del _self, worktree_path
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    class _BrowserValidation(_FakeValidation):
        def __init__(self) -> None:
            super().__init__(
                _validation_result(
                    tmp_path,
                    ok=False,
                    command="exit 1",
                    reason_code="POST_AGENT_FAILED",
                )
            )
            self.probe_calls: list[dict[str, object]] = []

        async def probe_runtime_browser_findings(
            self, **kwargs: object
        ) -> tuple[ProfileLintFinding, ...]:
            self.probe_calls.append(dict(kwargs))
            return (
                ProfileLintFinding(
                    reason_code=RUNTIME_BROWSER_UNAVAILABLE,
                    message="runtime does not provide Playwright browser chromium",
                    path="runtime.browsers",
                    severity=ProfileLintSeverity.warning,
                    details={"browser": "chromium", "available_browsers": []},
                ),
            )

    validation = _BrowserValidation()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=0,
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(pre_push_validation, "_pre_push_validation_cleanup", _cleanup)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert validation.probe_calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        browser_events = [
            event
            for event in ws.events
            if event.event_type == RUNTIME_BROWSER_UNAVAILABLE_EVENT_TYPE
        ]
    assert browser_events == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_committed_diff_error_blocks_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD committed-diff read failures must stop before validation."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "9" * 40
    recovered_head = "a" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=0, stdout="M\0pyproject.toml\0")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    cleanup_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        assert operation_start_head == recovery_base
        return recovered_head

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        del args, kwargs
        raise ProtectedScopeDiffError("committed diff unavailable")

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self
        cleanup_calls.append(
            {
                "worktree_path": worktree_path,
                "restore_ref": restore_ref,
            }
        )
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.workspace_head_sha == recovered_head
    assert result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "recovered HEAD diff unavailable" in result.message
    assert cleanup_calls == [
        {
            "worktree_path": worktree,
            "restore_ref": recovery_base,
        }
    ]
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_ownership_failure_restores_recovery_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered HEAD ownership failures must roll back before returning."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "9" * 40
    recovered_head = "a" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=0, stdout="M\0pyproject.toml\0")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    cleanup_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        assert operation_start_head == recovery_base
        return recovered_head

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return False

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        del args, kwargs
        raise AssertionError("protected-scope check should not run")

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self
        cleanup_calls.append(
            {
                "worktree_path": worktree_path,
                "restore_ref": restore_ref,
            }
        )
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.workspace_head_sha == recovered_head
    assert result.reason_code == "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
    assert "agent runtime ownership repair failed" in result.message
    assert cleanup_calls == [
        {
            "worktree_path": worktree,
            "restore_ref": recovery_base,
        }
    ]
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_ownership_repair_failure_blocks_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD ownership repair failures must stop before validation starts."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "7" * 40
    recovered_head = "8" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    committed_diff_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        assert operation_start_head == recovery_base
        return recovered_head

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return False

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        committed_diff_calls.append({"args": args, **kwargs})
        return []

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.workspace_head_sha == recovered_head
    assert result.reason_code == "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
    assert validation.calls == []
    assert committed_diff_calls == []
