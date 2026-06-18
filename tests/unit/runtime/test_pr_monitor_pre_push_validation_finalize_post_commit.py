"""Pre-push validation dirty-finalize and tail regression tests (part 2).

Split from ``test_pr_monitor_pre_push_validation`` to keep first-party files
under the maintainability line limit; see
``test_core_decomposition_maintainability``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.validation_types import ValidationResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
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
    _FakeValidation,
    _mark_git_worktree,
    _name_status_z,
    _provider_coverage_failure_without_command,
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
async def test_pre_push_validation_finalize_propagates_provider_recovery_retry(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A provider-recovery-retry finalize must propagate, not collapse into generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
    raises ``ProviderRecoveryRetryError`` when provider recovery suppresses the CLI
    and the operation must back off and retry later. The loop's
    ``except ProviderRecoveryRetryError`` handler surfaces ``PROVIDER_OUTAGE`` retry
    semantics, so the finalize must re-raise it instead of swallowing it (the broad
    ``except Exception`` previously returned ``None``, reusing the stale dirty check
    and reporting the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``) — regression
    for thread ``PRRT_kwDOSJAM6s6KWpSB``.
    """
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    # The committed delta is parsed from ``--name-status -z``; the staged delta
    # is unqueued and resolves to the default empty result (no staged paths).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderRecoveryRetryError()),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    with pytest.raises(ProviderRecoveryRetryError):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_propagates_provider_recovery_fallback(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A provider-recovery-fallback finalize must propagate, not collapse into generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
    ``_handle_provider_agent_run_error`` raises ``ProviderRecoveryFallbackError``
    when a provider failure triggers a fallback workspace. The loop's
    ``except ProviderRecoveryFallbackError`` handler surfaces ``PROVIDER_FALLBACK``
    semantics, so the finalize must re-raise it instead of swallowing it (the broad
    ``except Exception`` previously returned ``None``, reusing the stale dirty check
    and reporting the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``) —
    regression for thread ``PRRT_kwDOSJAM6s6KYd-t``.
    """
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryFallbackError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    # The committed delta is parsed from ``--name-status -z``; the staged delta
    # is unqueued and resolves to the default empty result (no staged paths).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderRecoveryFallbackError()),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    with pytest.raises(ProviderRecoveryFallbackError):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_propagates_provider_recovery_auth(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A provider-recovery-auth-failed finalize must propagate, not collapse into generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
    ``_handle_provider_agent_run_error`` raises ``ProviderRecoveryAuthError`` when
    provider auth is broken and the operation cannot continue. The loop's
    ``except ProviderRecoveryAuthError`` handler surfaces the auth-failed operation
    outcome, so the finalize must re-raise it instead of swallowing it (the broad
    ``except Exception`` previously returned ``None``, reusing the stale dirty check
    and reporting the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``) —
    regression for thread ``PRRT_kwDOSJAM6s6KYd-t``.
    """
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryAuthError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    # The committed delta is parsed from ``--name-status -z``; the staged delta
    # is unqueued and resolves to the default empty result (no staged paths).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderRecoveryAuthError()),
    )
    state = MonitorState()
    operation_start_head = "0" * 40

    with pytest.raises(ProviderRecoveryAuthError):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_pre_push_validation_finalize_rolls_back_dirty_residue_before_provider_recovery(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    exc_cls: str,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6KewGH (discussion r3434397656).

    When protected-scope repair inside
    ``_try_finalize_pre_push_dirty_repair_state`` raises a provider-recovery
    control-flow exception (``ProviderRecoveryRetryError`` /
    ``ProviderRecoveryFallbackError`` / ``ProviderRecoveryAuthError``), the
    finalize must roll back the dirty residue it was trying to finalize to the
    finalize-start HEAD BEFORE re-raising. Otherwise the outer monitor records
    a provider retry/fallback/auth outcome and exits the operation; on the next
    repair cycle the repair-start guard
    (``_pre_existing_dirty_repair_worktree_result``) sees the still-dirty
    worktree and fails as ``PRE_EXISTING_DIRTY_WORKTREE`` before the provider
    retry can actually run, so a transient provider outage wedges the workspace
    instead of retrying. Mirrors the fix-pass residue rollback in
    ``_run_pre_push_validation_fix_pass`` (PRRT_kwDOSJAM6s6Kc_Ak).
    """
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    finalize_start_head = "c" * 40
    cmd = FakeCommandRunner()
    # ``_run_pre_push_validation`` reads HEAD before the finalize call; that
    # SHA is threaded in as ``finalize_start_head`` (the rollback anchor
    # fallback) so the finalize does NOT issue a second ``rev-parse HEAD`` in
    # the success path — only the rollback (error) path issues extra git
    # commands.
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    # ``_operation_owned_delta_paths`` reads the committed delta
    # (``git diff --name-status -z operation_start_head..HEAD``); the dirty
    # path is operation-owned so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # ``_commit_dirty_worktree`` is mocked to raise; the protected-scope
    # repair agent in this scenario did NOT self-commit, so the
    # post-agent/pre-sink HEAD captured in the exception handler still equals
    # ``finalize_start_head`` and is used as the rollback anchor (preserving
    # the operation's committed work while discarding only the residue).
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``:
    # the protected-scope repair agent left the dirty residue behind.
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")
    # ``git restore --source <finalize_start_head> --staged --worktree -- src/fix.py``.
    cmd.queue_result(returncode=0)
    # Post-restore verify ``check_validation_worktree_clean`` (worktree now clean).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <finalize_start_head>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    raised_exc = getattr(monitor_types, exc_cls)(
        "provider recovery raised by protected-scope repair in finalize"
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))
    state = MonitorState()
    operation_start_head = "0" * 40

    # The provider-recovery exception must still propagate so the monitor
    # loop's dedicated handlers surface ``PROVIDER_OUTAGE`` /
    # ``PROVIDER_FALLBACK`` / auth-failed semantics — but only AFTER the
    # finalize residue has been rolled back.
    with pytest.raises(type(raised_exc)):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    # The finalize MUST roll back the residue to the finalize-start HEAD
    # before re-raising so the next monitor attempt does not trip
    # ``PRE_EXISTING_DIRTY_WORKTREE``.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        f"restore --source {finalize_start_head} --staged --worktree" in call
        for call in joined_calls
    ), joined_calls
    # The finalize failure must not re-check the tree via the pre-validation
    # check (no verify/recheck pass through ``_pre_push_validation_worktree_check``);
    # the rollback's own ``check_validation_worktree_clean`` runs inside
    # ``_pre_push_validation_cleanup`` and does not go through the patched
    # module-level helper.
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_pre_push_validation_finalize_provider_recovery_rolls_back_to_post_agent_head_not_finalize_start_head(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    exc_cls: str,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6KnWkn — preserve protected-scope repair self-commits.

    In the dirty-finalize path ``_commit_dirty_worktree`` ->
    ``_repair_protected_scope_changes_before_commit`` runs the protected-scope
    repair agent, which may self-commit and advance HEAD past
    ``finalize_start_head`` BEFORE a recoverable provider error is raised. The
    residue rollback must anchor against the post-agent/pre-sink HEAD (the
    advanced HEAD carrying the self-commit), NOT ``finalize_start_head``:
    ``_pre_push_validation_cleanup`` ->
    ``cleanup_validation_worktree_side_effects`` resets HEAD back to the
    restore_ref when it sees HEAD advanced, so anchoring to
    ``finalize_start_head`` discards the valid protected-scope repair
    self-commit and the provider retry then starts from the old tree and can
    lose or repeatedly redo the repair work. Mirrors the fix-pass rollback
    (``PRRT_kwDOSJAM6s6Klf78``) and the CI-repair rollback
    (``PRRT_kwDOSJAM6s6Klf74``), which both anchor against the
    post-agent/pre-sink HEAD for the same reason.
    """
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    finalize_start_head = "c" * 40
    # The protected-scope repair agent self-committed inside
    # ``_commit_dirty_worktree`` and advanced HEAD past
    # ``finalize_start_head`` before the provider-recovery exception was
    # raised. The rollback must anchor against THIS HEAD so the self-commit
    # is preserved.
    post_agent_head = "d" * 40
    cmd = FakeCommandRunner()
    # ``_run_pre_push_validation`` reads HEAD before the finalize call; that
    # SHA is threaded in as ``finalize_start_head``.
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    # ``_operation_owned_delta_paths`` reads the committed delta; the dirty
    # path is operation-owned so the finalize proceeds.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # ``_commit_dirty_worktree`` is mocked to raise after the protected-scope
    # repair agent self-committed, so no git calls happen inside it. The
    # exception handler captures the post-agent HEAD via ``_rev_parse_head``.
    cmd.queue_result(returncode=0, stdout=f"{post_agent_head}\n")
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``:
    # the protected-scope repair agent left the dirty residue behind.
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")
    # ``git restore --source <post_agent_head> --staged --worktree -- src/fix.py``.
    cmd.queue_result(returncode=0)
    # Post-restore verify ``check_validation_worktree_clean`` (worktree clean).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <post_agent_head>`` + ``rev-parse HEAD``.
    # Both resolve to ``post_agent_head`` so no ``git reset --hard`` runs and
    # the self-commit is preserved.
    cmd.queue_result(returncode=0, stdout=f"{post_agent_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{post_agent_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    raised_exc = getattr(monitor_types, exc_cls)(
        "provider recovery raised after protected-scope repair self-commit"
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(side_effect=raised_exc))
    state = MonitorState()
    operation_start_head = "0" * 40

    # The provider-recovery exception must still propagate, but only AFTER the
    # residue has been rolled back to the post-agent HEAD (preserving the
    # self-commit).
    with pytest.raises(type(raised_exc)):
        await pre_push_validation_module._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            operation_start_head=operation_start_head,
        )

    joined_calls = [" ".join(call.args) for call in cmd.calls]
    # The rollback MUST anchor against the post-agent HEAD, NOT
    # ``finalize_start_head``: restoring tracked paths to the advanced ref
    # preserves the self-commit, and the HEAD verification sees
    # ``current == restore_ref`` so no ``git reset --hard`` runs.
    assert any(
        f"restore --source {post_agent_head} --staged --worktree" in call for call in joined_calls
    ), joined_calls
    assert not any(
        f"restore --source {finalize_start_head} --staged --worktree" in call
        for call in joined_calls
    ), joined_calls
    assert not any("reset --hard" in call for call in joined_calls), joined_calls
    # The finalize failure must not re-check the tree via the pre-validation
    # check (no verify/recheck pass through ``_pre_push_validation_worktree_check``).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_reports_dirty_worktree_when_head_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Pre-existing dirt should not be hidden by a later HEAD capture failure."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message="dirty file prevents validation",
    )
    check_worktree_clean = AsyncMock(return_value=dirty_check)
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    rev_parse_head = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "_rev_parse_head", rev_parse_head)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.workspace_head_sha is None
    assert result.validation_run_id is None
    check_worktree_clean.assert_awaited_once()
    rev_parse_head.assert_awaited_once_with(worktree)


@pytest.mark.unit
async def test_pre_push_validation_worktree_check_installs_agent_scratch_excludes(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The monitor guard must install adapter scratch excludes before checking cleanliness.

    A monitor-adopted or resumed workspace may never have passed through the
    executor's scratch-exclude setup, yet the monitor's own fix-pass agent run
    can create ``.claude/worktrees/``. The pre-push worktree guard therefore has
    to (re)install the adapter's scratch excludes before judging cleanliness, or
    it would refuse the otherwise clean tree (regression for thread
    ``PRRT_kwDOSJAM6s6HjHiR``).
    """
    worktree = tmp_path / "worktrees" / "ws-scratch"

    class _ScratchAdapter(FakeAdapter):
        @property
        def runtime_scratch_paths(self) -> tuple[str, ...]:
            return (".claude/worktrees/",)

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=_ScratchAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    call_order: list[str] = []
    applied_scratch_paths: list[tuple[str, ...]] = []

    async def _spy_apply(
        *,
        run_git: Any,
        worktree_path: Path,
        scratch_paths: tuple[str, ...],
    ) -> bool:
        call_order.append("apply")
        applied_scratch_paths.append(scratch_paths)
        return True

    clean_check = ValidationWorktreeCheck(clean=True, reason_code=None, message=None)

    async def _spy_clean(**_kwargs: Any) -> ValidationWorktreeCheck:
        call_order.append("check")
        return clean_check

    monkeypatch.setattr(
        pre_push_validation_module,
        "apply_agent_scratch_excludes",
        _spy_apply,
    )
    monkeypatch.setattr(
        "awf.runtime.validation_worktree.check_validation_worktree_clean",
        _spy_clean,
    )

    result = await pre_push_validation_module._pre_push_validation_worktree_check(
        runner,
        worktree_path=worktree,
    )

    assert result is clean_check
    assert applied_scratch_paths == [(".claude/worktrees/",)]
    assert call_order == ["apply", "check"]


@pytest.mark.unit
async def test_pre_push_validation_coverage_provider_failure_without_command_skips_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Coverage provider failures without command records cannot run a fix pass."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'8' * 40}\n")
    validation = _FakeValidation(
        ValidationResult(coverage=_provider_coverage_failure_without_command())
    )
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
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
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == "COVERAGE_PROVIDER_FAILED"
    assert "failing_command" not in result.details
    assert "failing_returncode" not in result.details
    assert len(validation.calls) == 1
    assert adapter.calls == []
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_coverage_provider_skip_still_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A configured coverage provider may decline to emit a result."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id, include_coverage=True)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'9' * 40}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    validation = _FakeValidation(
        _validation_result(tmp_path, ok=True),
        coverage_result=None,
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
    assert len(validation.coverage_calls) == 1
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].status == "succeeded"
    assert runs[-1].coverage is None


@pytest.mark.unit
async def test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A finalize commit that introduces paths outside the operation delta must fail closed.

    The pre-push dirty finalize ownership gate is checked *before* calling
    ``_commit_dirty_worktree``, but that commit sink runs a fresh ``git status``,
    may invoke protected-scope repair (which runs the agent CLI), and then
    stages **all** non-ignored dirty paths. If a side effect between the gate
    check and the fresh staging scan creates an extra path outside
    ``owned_delta_paths``, the stale gate is bypassed and the unowned path is
    committed. The finalize must re-validate the operation's committed delta
    after the commit and fail closed with a dedicated reason code so the
    unowned commit is never silently pushed (regression for review thread
    ``PRRT_kwDOSJAM6s6KZP8f``).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
        _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    # Only the initial pre-validation check is expected; the post-commit
    # fail-closed branch must NOT re-run the worktree cleanliness check.
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit operation-owned delta: the dirty path is owned via the
    # committed delta, so the gate lets the finalize proceed. The staged delta
    # is NOT consulted (removed for PRRT_kwDOSJAM6s6KdVXx); the live
    # working-tree delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    # Post-commit re-validation: only the committed delta is re-checked. The
    # commit sink's side effects introduced an extra unowned path that was
    # committed, so the committed delta now carries both the operation-owned
    # path and the unowned extra path.
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("M\0src/fix.py\0", "M\0unrelated/extra.py\0"),
    )  # post-commit committed delta
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=True))
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON
    assert result.validation_run_id is None
    assert result.workspace_head_sha == "b" * 40
    # Validation must never run when the finalize fails closed on unowned delta.
    assert validation.calls == []
    # The post-commit fail-closed branch must not re-run the worktree check.
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_fail_closed_with_delta_unavailable_reason_when_post_commit_delta_missing(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A finalize whose post-commit committed delta cannot be inspected must use a dedicated reason.

    When ``_committed_delta_paths`` returns ``None`` after the finalize commit
    (because ``git diff`` failed or its ``--name-status -z`` output was
    malformed), the finalize must still fail closed — but the reason code must
    reflect that the committed delta could not be *inspected*, not that an
    unowned path was *committed*. Reusing
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` here would mislabel an
    un-inspectable commit as a proven-unowned commit and mislead operators
    (review thread ``PRRT_kwDOSJAM6s6KhtZJ``).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
        _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
        _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    # Only the initial pre-validation check is expected; the post-commit
    # fail-closed branch must NOT re-run the worktree cleanliness check.
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit operation-owned delta: the dirty path is owned via the
    # committed delta, so the gate lets the finalize proceed.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    # Post-commit re-validation: ``git diff`` fails, so the committed delta
    # cannot be inspected and ``_committed_delta_paths`` returns None.
    cmd.queue_result(returncode=1, stdout="", stderr="unknown revision")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=True))
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON
    assert result.reason_code != _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON
    assert result.validation_run_id is None
    assert result.workspace_head_sha == "b" * 40
    # Validation must never run when the finalize fails closed on an
    # un-inspectable committed delta.
    assert validation.calls == []
    # The post-commit fail-closed branch must not re-run the worktree check.
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_ignores_working_tree_only_unowned_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Post-commit re-validation must only inspect the committed delta.

    After a successful finalize commit, the post-commit safety check compares
    the committed delta against ``owned_delta_paths``. An unrelated tracked
    edit that remains *only* in the working tree (the finalize commit did not
    add it) must not be flagged as ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA``,
    because the finalize commit did not sweep it into the PR. Re-using the full
    ``_operation_owned_delta_paths`` union (which includes the
    commit-vs-working-tree diff) would flag that working-tree-only dirt and
    fail-closed on a valid finalize (regression for review thread
    ``PRRT_kwDOSJAM6s6Ka0aO``).

    Note: the live working-tree delta was removed from the pre-commit ownership
    gate by ``PRRT_kwDOSJAM6s6KbbE6`` (it over-broadened ownership to every
    tracked working-tree difference). The dirty path here is therefore owned
    via the committed delta, and the unrelated working-tree-only path is simply
    never consulted by the gate — the post-commit committed-delta-only
    re-validation remains the load-bearing guard against unowned commits.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only; the staged delta is NOT
    # consulted — removed for PRRT_kwDOSJAM6s6KdVXx; the live working-tree
    # delta is NOT consulted — removed for PRRT_kwDOSJAM6s6KbbE6): the dirty
    # path ``src/fix.py`` is operation-owned via the committed delta.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    # Post-commit re-validation inspects ONLY the committed delta. The finalize
    # commit added the operation-owned ``src/fix.py`` and did NOT commit the
    # unrelated working-tree-only ``unrelated/extra.py``, so the committed delta
    # carries only the owned path and the finalize must proceed to the verify
    # recheck (which observes a clean tree). The unrelated working-tree-only
    # path must not be flagged as ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA``.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
    )
    cleanup.assert_awaited_once()
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_commits_operation_owned_rename_source_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned rename source dirt must be finalized, not stranded as pre-existing dirty.

    When a repair leaves a committed rename (e.g. ``git add -A`` and
    ``git commit`` both succeeded, moving ``oldname.txt`` to ``newname.txt``
    since ``operation_start_head``), ``check_validation_worktree_clean`` parses
    ``git status --porcelain`` and ``changed_paths_from_porcelain`` yields
    *both* the rename source (``oldname.txt``) and destination
    (``newname.txt``). ``_operation_owned_delta_paths`` must build the
    operation-owned set from ``git diff --name-status -z`` (which emits both
    names for ``R``/``C`` records and never C-quotes paths under ``-z``), not
    from raw ``git diff --name-only`` (which only yields the destination). If
    the owned set omits the rename source, ``unrelated_dirty = dirty_paths -
    owned_delta_paths`` treats the operation's own rename source as unrelated
    dirt and the finalize fails as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    instead of finalizing the operation's own rename (review thread
    ``PRRT_kwDOSJAM6s6KaAWk``).

    Note: the rename was previously exercised via the staged delta, but the
    staged delta was removed for ``PRRT_kwDOSJAM6s6KdVXx`` (it over-broadened
    ownership to whatever is in the live index at finalization time). The
    committed delta carries the same ``--name-status -z`` rename record, so
    the KaAWk path-representation concern is still covered here.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Porcelain reports the rename as ``R  oldname.txt -> newname.txt``, and
    # ``changed_paths_from_porcelain`` yields both names.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("oldname.txt", "newname.txt"),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only): the operation
    # committed the rename since ``operation_start_head``, so the committed
    # delta carries the rename record, which ``--name-status -z`` emits as both
    # the source and destination. The staged delta is NOT consulted (removed
    # for PRRT_kwDOSJAM6s6KdVXx); the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("R100\0oldname.txt\0newname.txt\0"),
    )
    # Post-commit re-validation inspects ONLY the committed delta: the rename
    # is still confined to the operation-owned set.
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("R100\0oldname.txt\0newname.txt\0"),
    )
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
    )
    cleanup.assert_awaited_once()


@pytest.mark.unit
async def test_pre_push_validation_finalize_commits_operation_owned_non_ascii_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned non-ASCII dirt must be finalized, not stranded as pre-existing dirty.

    With ``core.quotepath=true`` (the git default), ``git status --porcelain``
    C-quotes non-ASCII paths, but ``changed_paths_from_porcelain`` already
    unquotes them via ``unquote_porcelain_path``, so the dirty set carries
    the decoded path (``caf\\u00e9.txt``). ``git diff --name-only`` also
    C-quotes non-ASCII paths, and the raw line parsing in
    ``_operation_owned_delta_paths`` did not unquote them, so the owned set
    held the C-quoted form and never matched the decoded dirty path — the
    operation's own non-ASCII dirt was stranded as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``. ``git diff --name-status -z``
    never C-quotes paths (the ``-z`` form always emits raw bytes), so parsing
    that output yields the same decoded path representation the dirty check
    uses (review thread ``PRRT_kwDOSJAM6s6KaAWk``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # The porcelain parser already decoded the C-quoted ``"caf\\303\\251.txt"``
    # form, so the dirty set carries the decoded UTF-8 path.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("caf\u00e9.txt",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only): the operation committed
    # the non-ASCII path since ``operation_start_head``, so the committed
    # delta carries it as raw UTF-8 bytes (``--name-status -z`` never
    # C-quotes paths). The staged delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KdVXx); the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0caf\u00e9.txt\0"))
    # Post-commit re-validation inspects ONLY the committed delta: the
    # non-ASCII path is still confined to the operation-owned set.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0caf\u00e9.txt\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
    )
    cleanup.assert_awaited_once()


@pytest.mark.unit
async def test_pre_push_validation_finalize_strands_operation_owned_untracked_dirt_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned purely untracked dirt strands fail-closed (deferred).

    ``git diff --name-status -z`` (committed, staged, and working-tree) cannot
    see a purely untracked path: the agent created the file but ``git add -A``
    never reached it, so it is not staged, not committed, and absent from the
    commit-vs-working-tree diff. The pre-push cleanliness check uses
    ``git status --porcelain``, which DOES list untracked files, so the dirty
    set carries the path while the operation-owned delta computed from diffs is
    empty.

    ``PRRT_kwDOSJAM6s6Ka0aK`` previously recovered this case by folding
    ``check.untracked_paths`` into ``owned_delta_paths``. But the repair-start
    dirty guard only proves the worktree was clean at ``operation_start_head``
    at repair *start*; ``check.untracked_paths`` is computed at pre-push
    validation time, which is later. A failed cleanup or another local process
    can create an untracked file in that window, and the fold-in treated it as
    operation-owned solely because it was untracked — ``_commit_dirty_worktree``
    then staged it via ``git add -A`` and the post-commit re-validation saw it as
    committed and confined, silently sweeping the unrelated untracked file into
    the PR instead of failing closed (review thread
    ``PRRT_kwDOSJAM6s6KcSj``). The untracked fold-in was therefore removed and
    this recovery now strands the operation's own purely-untracked repair
    output as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` — fail-closed and
    visible to a human, rather than a silent sweep of unrelated dirt. Restoring
    this recovery without the over-broadening requires capturing only the
    operation's attempted untracked paths and threading them to the gate;
    tracked as a deferred follow-up (see
    ``plans/PRRT_kwDOSJAM6s6KcSj_PLAN.md``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Porcelain reports the purely untracked file as ``?? src/fix.py``; it is
    # not agent-runtime-ignored so it stays in both ``paths`` and
    # ``untracked_paths``.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        untracked_paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate: the committed and staged deltas are empty
    # because the path is purely untracked — not committed, not staged. The
    # live working-tree delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KbbE6). The untracked fold-in is NOT applied (removed for
    # PRRT_kwDOSJAM6s6KcSj), so the purely-untracked path is treated as
    # unrelated and the finalize skips — fail-closed, not a silent sweep.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    cmd.queue_result(returncode=0, stdout="")  # staged delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The operation-owned purely-untracked repair output strands fail-closed
    # (deferred recovery) — the commit sink must not run.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_excludes_agent_runtime_untracked_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Agent-runtime untracked dirt must stay fail-closed, not be swept into the PR.

    ``check_validation_worktree_clean`` suppresses AWF-agent-runtime artifacts
    (``.claude/agent-memory/``) unconditionally, so an untracked memory file
    never appears in ``check.paths`` nor ``check.untracked_paths``. Folding
    ``check.untracked_paths`` into the owned set must not re-introduce those
    artifacts (they are already absent), and a purely untracked memory file
    must stay fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` rather
    than be committed into the PR (review thread ``PRRT_kwDOSJAM6s6Ka0aK``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # The dirty check suppresses the agent-runtime untracked file, so neither
    # ``paths`` nor ``untracked_paths`` carry it; the gate has nothing to own
    # and must stay fail-closed.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(".claude/agent-memory/reviewer.json",),
        untracked_paths=(),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The committed and staged deltas are empty (the path is purely untracked)
    # AND ``check.untracked_paths`` is empty (the dirty check suppressed it),
    # so the owned set is empty and the finalize must skip and fail closed —
    # the agent-runtime artifact must never be committed into the PR. The live
    # working-tree delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    cmd.queue_result(returncode=0, stdout="")  # staged delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The agent-runtime artifact must not be committed into the PR.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_unrelated_untracked_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unrelated purely-untracked dirt must not be swept into the PR.

    The repair-start dirty guard
    (``_pre_existing_dirty_repair_worktree_result``) proves the worktree was
    clean at ``operation_start_head`` at repair *start*, but the pre-push
    cleanliness check (``check_validation_worktree_clean``) computes
    ``check.untracked_paths`` at pre-push validation time, which is later. A
    failed cleanup or another local process can create an untracked file in
    that window; it is NOT a path the operation captured or attempted (it is
    neither committed nor staged).

    ``PRRT_kwDOSJAM6s6Ka0aK`` folded ``check.untracked_paths`` into
    ``owned_delta_paths`` solely because the worktree was clean at
    ``operation_start_head``, treating every current untracked path as
    operation-owned. That let ``_commit_dirty_worktree`` stage the unrelated
    untracked file via ``git add -A`` and the post-commit re-validation see it
    as committed and confined to the owned set, silently sweeping the
    unrelated file into the PR instead of failing closed (review thread
    ``PRRT_kwDOSJAM6s6KcSj``). The untracked fold-in is removed for the same
    reason the live working-tree delta was removed
    (``PRRT_kwDOSJAM6s6KbbE6``): silent over-broadening is worse than visible
    fail-closed.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # A failed cleanup or another local process created this purely-untracked
    # file AFTER the repair-start guard, between the operation's own committed
    # edits. Porcelain reports it as ``?? unrelated/cleanup.log``; it is not
    # agent-runtime-ignored so it stays in both ``paths`` and
    # ``untracked_paths``. The operation's own edits were committed, so the
    # worktree would otherwise be clean — this untracked file is the only dirt.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("unrelated/cleanup.log",),
        untracked_paths=("unrelated/cleanup.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate: the committed and staged deltas are empty
    # relative to ``operation_start_head`` because the unrelated untracked file
    # is purely untracked (not committed, not staged). The live working-tree
    # delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6), and the
    # untracked fold-in is NOT applied (removed for PRRT_kwDOSJAM6s6KcSj), so
    # the unrelated untracked path is treated as unrelated dirt and the
    # finalize skips — fail-closed, not a silent sweep.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    cmd.queue_result(returncode=0, stdout="")  # staged delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The unrelated untracked file must NOT be committed into the PR.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1
