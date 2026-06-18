"""Pre-push validation fix-pass and repair flow tests (part 4).

Split from part 2 to keep first-party files under the maintainability line
limit; see ``test_core_decomposition_maintainability``. Holds the
``PRRT_kwDOSJAM6s6Klf78`` regression tests that prove the commit-sink
residue rollbacks anchor against the post-agent/pre-sink HEAD
(``post_agent_head``), not ``fix_start_head``, so a self-committed
validation fix is preserved when the sink raises a control-flow exception.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _mark_git_worktree,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_pre_push_validation_fix_pass_provider_recovery_rolls_back_to_post_agent_head_not_fix_start_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: str,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Klf78 — preserve self-committed validation fixes.

    When the validation-fix agent self-commits its fix and advances HEAD past
    ``fix_start_head``, and then ``_commit_dirty_worktree`` raises a
    provider-recovery control-flow exception (from
    ``_repair_protected_scope_changes_before_commit``) BEFORE making its own
    commit, the residue rollback must anchor against the post-agent/pre-sink
    HEAD (``post_agent_head``), NOT ``fix_start_head``. Resetting to
    ``fix_start_head`` would discard the agent's already-committed validation
    fix along with the stranded protected-scope residue, so the provider retry
    would start from the old tree and redo (or lose) valid repair work. This
    mirrors the CI-repair rollback (``PRRT_kwDOSJAM6s6Klf74``) and the finalize
    rollback, which both anchor against the post-agent/pre-sink HEAD for the
    same reason.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "7" * 40
    # The validation-fix agent committed its own fix and advanced HEAD past
    # ``fix_start_head`` before the commit sink ran. The rollback must
    # preserve this commit, not discard it back to ``fix_start_head``.
    agent_commit_head = "9" * 40
    # ``_run_pre_push_validation_fix_pass`` reads HEAD before the agent run.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # Post-agent/pre-sink HEAD (``post_agent_head``): the agent self-committed
    # and advanced HEAD to ``agent_commit_head``.
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    # ``_rollback_failed_pre_push_validation_fix_pass`` -> ``reset --hard``
    # MUST target ``agent_commit_head`` (NOT ``fix_start_head``).
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {agent_commit_head[:8]}\n")
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``
    # (status): report the protected-scope residue the agent left behind.
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    # ``git restore --source <agent_commit_head> --staged --worktree -- <path>``.
    cmd.queue_result(returncode=0)
    # Post-restore status recheck (no more residue after the restore).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <agent_commit_head>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="self-committed fix then left protected residue\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    raised_exc = getattr(monitor_types, exc_cls)(
        "provider recovery raised by protected-scope repair"
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate protected-scope repair raising a provider-recovery exception."""
        raise raised_exc

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    # The provider-recovery exception must still propagate so the monitor
    # loop's dedicated handlers surface ``PROVIDER_OUTAGE`` /
    # ``PROVIDER_FALLBACK`` / auth-failed semantics — but only AFTER the
    # fix-pass residue has been rolled back to the post-agent/pre-sink HEAD.
    with pytest.raises(type(raised_exc)):
        await pre_push_validation._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=None,
            validation_result=validation_result,
            pass_number=1,
            total_passes=1,
            validation_commands=("pytest -q",),
        )

    # The rollback MUST reset to the post-agent/pre-sink HEAD
    # (``agent_commit_head``), NOT ``fix_start_head`` — preserving the
    # validation-fix agent's already-committed fix so the provider retry
    # starts from the agent-advanced tree instead of redoing or losing valid
    # work.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        "reset" in call and "--hard" in call and agent_commit_head in call for call in joined_calls
    ), joined_calls
    assert not any(
        "reset" in call and "--hard" in call and fix_start_head in call for call in joined_calls
    ), joined_calls


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_policy_blocked_rolls_back_to_post_agent_head_not_fix_start_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Klf78 — preserve self-committed validation fixes (policy-blocked).

    Same as the provider-recovery variant, but for ``_MonitorPolicyBlockedError``:
    the agent self-commits its fix and advances HEAD past ``fix_start_head``,
    then ``_commit_dirty_worktree`` raises the policy-blocked exception from
    the supply-chain check that runs BEFORE the actual ``git commit``. The
    residue rollback must anchor against ``post_agent_head`` (preserving the
    agent's already-committed fix), NOT ``fix_start_head``. Mirrors the
    provider-recovery residue rollback above and the CI-repair rollback
    (``PRRT_kwDOSJAM6s6Klf74``).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "8" * 40
    # The validation-fix agent committed its own fix and advanced HEAD past
    # ``fix_start_head`` before the commit sink ran. The rollback must
    # preserve this commit, not discard it back to ``fix_start_head``.
    agent_commit_head = "0" * 40
    # ``_run_pre_push_validation_fix_pass`` reads HEAD before the agent run.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # Post-agent/pre-sink HEAD (``post_agent_head``): the agent self-committed
    # and advanced HEAD to ``agent_commit_head``.
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    # ``_rollback_failed_pre_push_validation_fix_pass`` -> ``reset --hard``
    # MUST target ``agent_commit_head`` (NOT ``fix_start_head``).
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {agent_commit_head[:8]}\n")
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``
    # (status): report the protected-scope residue the agent left behind.
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    # ``git restore --source <agent_commit_head> --staged --worktree -- <path>``.
    cmd.queue_result(returncode=0)
    # Post-restore status recheck (no more residue after the restore).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <agent_commit_head>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{agent_commit_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="self-committed fix then left protected residue\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    raised_exc = monitor_types._MonitorPolicyBlockedError("supply-chain policy blocked the commit")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate the policy check raising before the actual commit."""
        raise raised_exc

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    # The policy exception must still propagate so the monitor loop's
    # dedicated handler surfaces ``MONITOR_POLICY_BLOCKED`` semantics — but
    # only AFTER the fix-pass residue has been rolled back to the
    # post-agent/pre-sink HEAD.
    with pytest.raises(monitor_types._MonitorPolicyBlockedError):
        await pre_push_validation._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=None,
            validation_result=validation_result,
            pass_number=1,
            total_passes=1,
            validation_commands=("pytest -q",),
        )

    # The rollback MUST reset to the post-agent/pre-sink HEAD
    # (``agent_commit_head``), NOT ``fix_start_head`` — preserving the
    # validation-fix agent's already-committed fix so the next monitor
    # attempt starts from the agent-advanced tree instead of redoing or
    # losing valid work.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        "reset" in call and "--hard" in call and agent_commit_head in call for call in joined_calls
    ), joined_calls
    assert not any(
        "reset" in call and "--hard" in call and fix_start_head in call for call in joined_calls
    ), joined_calls


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_provider_recovery_rollback_skipped_when_post_agent_head_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Klf78 — missing anchor skips the reset.

    If the post-agent/pre-sink HEAD cannot be resolved (``git rev-parse HEAD``
    fails or returns empty), the residue rollback must be SKIPPED instead of
    restoring against the wrong ref (``fix_start_head``), mirroring the
    CI-repair and finalize rollbacks' ``restore_ref is None`` guards. A
    missing anchor makes a safe ``git reset --hard`` impossible — better to
    strand visibly than discard the agent's committed work against the wrong
    baseline. The provider-recovery exception still propagates so the loop's
    handlers run.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "7" * 40
    # ``_run_pre_push_validation_fix_pass`` reads HEAD before the agent run.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # Post-agent/pre-sink HEAD resolution FAILS (rev-parse errors) ->
    # ``_rev_parse_head`` returns None, so the rollback is skipped.
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="self-committed fix then left protected residue\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    raised_exc = monitor_types.ProviderRecoveryRetryError(
        "provider recovery raised by protected-scope repair"
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate protected-scope repair raising a provider-recovery exception."""
        raise raised_exc

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass._log.warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    with pytest.raises(monitor_types.ProviderRecoveryRetryError):
        await pre_push_validation._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=None,
            validation_result=validation_result,
            pass_number=1,
            total_passes=1,
            validation_commands=("pytest -q",),
        )

    # No ``git reset --hard`` runs — the missing anchor makes a safe restore
    # impossible, so the residue strands visibly instead of being discarded
    # against the wrong ref.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("reset" in call and "--hard" in call for call in joined_calls), joined_calls
    # The skip is logged so triage can see why the rollback was not attempted.
    assert any(
        event == "monitor.pre_push_validation_fix_rollback_skipped_no_anchor"
        for event, _ in warnings
    ), warnings
