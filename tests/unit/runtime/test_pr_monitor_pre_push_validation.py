"""Regression tests for PR monitor pre-push validation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.validation_types import ValidationCommandResult, ValidationResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _FakeValidation:
    def __init__(self, *results: ValidationResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def run_profile_phases(self, **kwargs: object) -> ValidationResult:
        self.calls.append(dict(kwargs))
        if not self.results:
            raise AssertionError("validation called more times than expected")
        return self.results.pop(0)

    async def run_profile_coverage(self, **_kwargs: object) -> None:
        return None


def _command_result(tmp_path: Path, *, ok: bool) -> ValidationCommandResult:
    stdout_path = tmp_path / ("ok.stdout" if ok else "failed.stdout")
    stderr_path = tmp_path / ("ok.stderr" if ok else "failed.stderr")
    stdout_path.write_text("passed\n" if ok else "failed\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest -q",
        returncode=0 if ok else 1,
        duration_seconds=0.1,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        reason_code="VALIDATION_OK" if ok else "PYTEST_TEST_FAILURE",
    )


def _validation_result(tmp_path: Path, *, ok: bool) -> ValidationResult:
    return ValidationResult(commands=[_command_result(tmp_path, ok=ok)])


async def _set_resolved_profile(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "test-profile",
            "phases": {"validate": ["pytest -q"]},
        }
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = profile.model_dump(mode="json", by_alias=True)
        await session.commit()


async def _validation_runs(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[Any]:
    async with factory() as session:
        return await ValidationRunRepository(session).list_for_workspace(workspace_id)


@pytest.mark.unit
async def test_pre_push_validation_records_target_head_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    local_head = "f" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    runs = await _validation_runs(factory, workspace_id)
    pre_push_run = runs[-1]
    assert pre_push_run.workspace_head_sha == local_head
    assert pre_push_run.target_head_sha == local_head
    assert pre_push_run.target_branch == f"awf/{workspace_id}"
    assert pre_push_run.status == "succeeded"


@pytest.mark.unit
async def test_pre_push_validation_failure_does_not_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]
    assert result.details is not None
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_revalidates_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "b" * 40
    fixed_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[str] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        committed.append(str(kwargs["message"]))
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert committed == [f"awf: pre-push validation fix for {workspace_id}"]
    assert len(adapter.calls) == 1
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].target_head_sha == fixed_head


@pytest.mark.unit
async def test_comment_repair_uses_validated_push_and_does_not_resolve_on_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_validation",
        path="src/foo.py",
        line=1,
        body_excerpt="please fix",
        author="reviewer",
    )
    state = MonitorState()
    calls: list[str] = []

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return ("start", None)

    async def _address(**_kwargs: object) -> str:
        return "fix_committed"

    async def _clean_status(**_kwargs: object) -> object:
        return PRStatus(
            number=42,
            head_sha="start",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        calls.append("validated")
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="validation failed",
            reason_code="PRE_PUSH_VALIDATION_FAILED",
        )

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("comment repair must not call raw push")

    async def _unexpected_resolve(**_kwargs: object) -> None:
        pytest.fail("threads must not be resolved when validation blocks push")

    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _clean_status)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _unexpected_resolve)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert calls == ["validated"]
    assert "T_validation" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_ci_repair_uses_validated_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    calls: list[str] = []

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)

    async def _provider_allows_cli(*_args: object) -> bool:
        return False

    monkeypatch.setattr(runner, "_provider_recovery_suppresses_cli", _provider_allows_cli)

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return ("start", None)

    async def _commit(**_kwargs: object) -> bool:
        return True

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        calls.append("validated")
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("CI repair must not call raw push")

    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(
            CheckFailure(
                name="ci",
                conclusion="FAILURE",
                log_excerpt="failed",
            ),
        ),
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        state=MonitorState(),
    )

    assert result.failed is False
    assert calls == ["validated"]


@pytest.mark.unit
async def test_sync_base_uses_validated_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # merge --no-edit origin/development
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    calls: list[str] = []

    async def _fetch_base(**_kwargs: object) -> None:
        return None

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        calls.append("validated")
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("sync-base repair must not call raw push")

    monkeypatch.setattr(runner, "_fetch_base", _fetch_base)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_sync_base(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert calls == ["validated"]
