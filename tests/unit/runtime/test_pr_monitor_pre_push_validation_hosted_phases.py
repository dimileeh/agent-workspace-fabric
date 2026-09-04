"""Hosted pre-push validation must run setup+coverage in one ephemeral Job."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor.helpers import _pre_push_validation_phase_names
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.pr_monitor_runner.constants import (
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from awf.runtime.validation_worktree import (
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _coverage_result,
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


@pytest.mark.unit
def test_pre_push_validation_phase_names_local_vs_hosted() -> None:
    """Hosted pre-push includes setup+pre_agent; local keeps post_agent+validate."""
    assert _pre_push_validation_phase_names(is_hosted=False) == ("post_agent", "validate")
    assert _pre_push_validation_phase_names(is_hosted=True) == (
        "setup",
        "pre_agent",
        "post_agent",
        "validate",
    )


@pytest.mark.unit
async def test_hosted_pre_push_runs_setup_post_agent_validate_with_coverage_in_one_job(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Hosted pre-push uses one phases call with setup and include_coverage."""
    workspace_id = await seed_monitoring_workspace(factory, pr_number=277)
    await _set_resolved_profile(
        factory,
        workspace_id,
        include_coverage=True,
        setup_commands=["npm ci"],
        post_agent_commands=["npm run format"],
        validate_commands=["npm run lint"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    coverage = _coverage_result(tmp_path)
    validation = _FakeValidation(
        _validation_result(
            tmp_path,
            ok=True,
            command="npm run lint",
            coverage=coverage,
            commands=[
                _validation_result(
                    tmp_path, ok=True, command="npm ci", phase="setup", artifact_name="setup"
                ).commands[0],
                _validation_result(
                    tmp_path,
                    ok=True,
                    command="npm run format",
                    phase="post_agent",
                    artifact_name="post_agent",
                ).commands[0],
                _validation_result(
                    tmp_path,
                    ok=True,
                    command="npm run lint",
                    phase="validate",
                    artifact_name="validate",
                ).commands[0],
            ],
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.calls) == 1
    assert len(validation.coverage_calls) == 0
    phase_call = validation.calls[0]
    assert phase_call["phase_names"] == ("setup", "pre_agent", "post_agent", "validate")
    assert phase_call["include_coverage"] is True
    assert phase_call["run_healthchecks"] is True
    assert isinstance(phase_call["pr_identity"], dict)
    assert phase_call["pr_identity"]["pr_number"] == 277
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "succeeded"
    phases = [cmd.get("phase") for cmd in runs[-1].commands]
    assert phases[:3] == ["setup", "post_agent", "validate"]
    assert "coverage" in phases
    assert runs[-1].coverage is not None
    assert runs[-1].coverage["percent"] == 99.5
    assert runs[-1].coverage["reason_code"] == "COVERAGE_OK"


@pytest.mark.unit
async def test_hosted_pre_push_rejects_passing_result_that_omits_requested_coverage(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Hosted include_coverage must not push when the combined result lacks coverage."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(
        factory,
        workspace_id,
        include_coverage=True,
        setup_commands=["npm ci"],
        validate_commands=["npm run lint"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "f" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    # Passing phases with coverage=None simulates an incomplete combined response.
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=0,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED"
    assert len(validation.calls) == 1
    assert validation.calls[0]["include_coverage"] is True
    assert len(validation.coverage_calls) == 0
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_hosted_pre_push_setup_failure_blocks_later_phases_and_coverage(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failing hosted setup phase must block push without a coverage Job."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(
        factory,
        workspace_id,
        include_coverage=True,
        setup_commands=["npm ci"],
        validate_commands=["npm run lint"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "b" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(
        _validation_result(
            tmp_path,
            ok=False,
            command="npm ci",
            phase="setup",
            reason_code="COMMAND_FAILED",
            artifact_name="setup_fail",
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=0,
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert len(validation.calls) == 1
    assert validation.calls[0]["phase_names"] == (
        "setup",
        "pre_agent",
        "post_agent",
        "validate",
    )
    assert validation.calls[0]["include_coverage"] is True
    assert len(validation.coverage_calls) == 0
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_hosted_pre_push_missing_head_recovery_evidence_includes_setup(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-HEAD recovery command evidence must include hosted setup commands."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(
        factory,
        workspace_id,
        setup_commands=["npm ci"],
        post_agent_commands=["npm run format"],
        validate_commands=["npm run lint"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "1" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    recovery_calls: list[dict[str, object]] = []

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
        del self, worktree_path, task_tag
        assert operation_start_head == recovery_base
        recovery_calls.append(
            {
                "workspace_id": workspace_id,
                "command_evidence": command_evidence,
            }
        )
        raise _MonitorPolicyBlockedError(
            "PROTECTED_SCOPE_REPAIR_FAILED (.github/workflows/ci.yml)",
            reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
        )

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
        trusted_index_symlinks_are_symlinks: bool | None = None,
    ) -> ValidationWorktreeCleanup:
        del self, worktree_path, trusted_index_symlinks_are_symlinks
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=False, paths=("package-lock.json",)),
            restore_ref=restore_ref,
            cleaned_paths=("package-lock.json",),
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
    assert result.reason_code == _PROTECTED_SCOPE_REPAIR_FAILED_REASON
    assert recovery_calls == [
        {
            "workspace_id": workspace_id,
            "command_evidence": ("npm ci", "npm run format", "npm run lint"),
        }
    ]
    assert validation.calls == []


@pytest.mark.unit
async def test_hosted_pre_push_empty_setup_phase_still_requests_setup(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Hosted profiles without setup commands still request the setup phase once."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(
        factory,
        workspace_id,
        setup_commands=[],
        validate_commands=["pytest -q"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.calls) == 1
    assert validation.calls[0]["phase_names"] == (
        "setup",
        "pre_agent",
        "post_agent",
        "validate",
    )
    assert validation.calls[0]["include_coverage"] is False
    assert len(validation.coverage_calls) == 0
    runs = await _validation_runs(factory, workspace_id)
    phases = [cmd.get("phase") for cmd in runs[-1].commands]
    assert "setup" not in phases
    assert "validate" in phases


@pytest.mark.unit
async def test_hosted_pre_push_skips_coverage_when_final_gate_is_none(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Hosted coverage.command without final_gate coverage must not gate pre-push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(
        factory,
        workspace_id,
        include_coverage=True,
        coverage_final_gate="none",
        setup_commands=["npm ci"],
        validate_commands=["npm run lint"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.calls) == 1
    assert validation.calls[0]["include_coverage"] is False
    assert len(validation.coverage_calls) == 0


@pytest.mark.unit
async def test_local_pre_push_unchanged_phases_and_separate_coverage(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Local pre-push still skips setup and runs coverage as a separate call."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(
        factory,
        workspace_id,
        include_coverage=True,
        setup_commands=["npm ci"],
        validate_commands=["pytest -q"],
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=_coverage_result(tmp_path),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(validation.calls) == 1
    assert validation.calls[0]["phase_names"] == ("post_agent", "validate")
    assert validation.calls[0]["include_coverage"] is False
    assert len(validation.coverage_calls) == 1
    assert validation.coverage_calls[0]["phase"] == "coverage"
