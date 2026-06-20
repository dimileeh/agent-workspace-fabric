"""Additional edge coverage for PR monitor pre-push validation helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _mark_git_worktree,
    _set_resolved_profile,
    _validation_result,
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
