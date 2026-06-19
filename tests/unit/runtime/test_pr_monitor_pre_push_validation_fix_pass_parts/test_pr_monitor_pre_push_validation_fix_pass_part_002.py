"""Pre-push validation fix-pass and repair flow tests (part 2).

Split from the original module to keep first-party files under the
maintainability line limit; see ``test_core_decomposition_maintainability``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _FakeValidation,
    _mark_git_worktree,
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
async def test_pre_push_validation_fix_pass_rolls_back_when_commit_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed validation-fix commit must not leave staged changes for the next repair."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # Re-read HEAD after a clean (no-commit) fix pass: HEAD did not advance, so
    # the fix pass is classified as a genuine no-op and rolled back. The
    # commit-sink ``except`` clauses capture HEAD INSIDE each clause (after the
    # sink raised), not before the sink (review thread
    # ``PRRT_kwDOSJAM6s6Klf78`` / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a validation-fix commit failure."""
        return False

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

    committed, rollback_failed = await pre_push_validation._run_pre_push_validation_fix_pass(
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

    assert committed is False
    assert rollback_failed is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert any("clean -ffd" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_without_failure_returns_false() -> None:
    """A validation result with no command failure should not invoke a fix agent."""
    validation_result = pre_push_validation_module._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_provider",
        workspace_head_sha="a" * 40,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="coverage provider failed",
        validation_reason_code="COVERAGE_PROVIDER_FAILED",
        result=ValidationResult(coverage=_provider_coverage_failure_without_command()),
    )

    committed, rollback_failed = await pre_push_validation_module._run_pre_push_validation_fix_pass(
        object(),
        workspace_id="ws_provider",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        remote_branch="awf/ws_provider",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=(),
    )

    assert committed is False
    assert rollback_failed is None


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rolls_back_when_commit_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit-path exception should not strand the fix-pass worktree delta."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "9" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # Post-raise HEAD (captured INSIDE the generic commit-sink ``except``
    # clause after the sink raised): the agent did not self-commit, so it
    # equals ``fix_start_head``. The generic commit-sink exception rollback
    # anchors against this (review thread ``PRRT_kwDOSJAM6s6Klf78`` /
    # ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a validation-fix commit failure."""
        raise RuntimeError("commit path failed")

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

    committed, rollback_failed = await pre_push_validation._run_pre_push_validation_fix_pass(
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

    assert committed is False
    assert rollback_failed is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert any("clean -ffd" in call for call in joined_calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls",
    [
        "ProtectedScopeDiffError",
        "_MonitorAgentRuntimeOwnershipRepairFailedError",
        "_MonitorHeadObjectMissingError",
        "_MonitorMirrorHooksPathRepairFailedError",
    ],
)
async def test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: str,
) -> None:
    """Deterministic reason-coded exceptions from ``_commit_dirty_worktree`` must propagate, not collapse into ``commit_exception``.

    The provider-recovery exceptions (``ProviderRecoveryRetryError`` /
    ``ProviderRecoveryFallbackError`` / ``ProviderRecoveryAuthError``) are
    covered separately by
    ``test_pre_push_validation_fix_pass_rolls_back_dirty_residue_before_provider_retry``:
    those roll back the fix-pass residue BEFORE re-raising
    (``PRRT_kwDOSJAM6s6Kc_Ak``). ``_MonitorPolicyBlockedError`` is also covered
    separately (``PRRT_kwDOSJAM6s6Kg7Dm``): it is non-terminal so it must roll
    back its dirty residue before re-raising too. The exceptions here represent
    deterministic commit-sink failures that must keep the plain re-raise (no
    rollback), so the caller can surface their structured reason codes.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "9" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # These terminal reason-coded exceptions re-raise WITHOUT rolling back, so
    # no post-raise HEAD capture runs in their ``except`` clause. The
    # commit-sink ``except`` clauses that DO roll back capture HEAD INSIDE
    # each clause (after the sink raised), not before the sink (review thread
    # ``PRRT_kwDOSJAM6s6Klf78`` / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    raised_exc = getattr(monitor_types, exc_cls)("reason-coded failure")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
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

    # The rollback handler must NOT run for the TERMINAL reason-coded exceptions
    # when the worktree is clean (no residue to clean): no ``reset --hard``
    # against ``fix_start_head`` should be queued.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rolls_back_dirty_residue_before_policy_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Kg7Dm (discussion r3435181747).

    ``_commit_dirty_worktree`` raises ``_MonitorPolicyBlockedError`` from the
    supply-chain policy check that runs BEFORE the actual ``git commit``, so
    the agent's fix-pass residue is still dirty in the worktree. The parent
    converts the exception to a ``MONITOR_POLICY_BLOCKED`` push failure, which
    is intentionally NON-terminal: the monitor loop increments and retries.
    Re-raising without rolling back strands the residue, and the next cycle's
    repair-start dirty guard (``_pre_existing_dirty_repair_worktree_result``)
    trips as ``PRE_EXISTING_DIRTY_WORKTREE``, losing the policy reason and
    wedging recovery instead of re-polling cleanly. The fix-pass must roll
    back the residue to ``fix_start_head`` BEFORE re-raising, mirroring the
    provider-recovery residue rollback
    (``test_pre_push_validation_fix_pass_rolls_back_dirty_residue_before_provider_retry``).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "8" * 40
    # ``_run_pre_push_validation_fix_pass`` reads HEAD before the agent run.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    # Post-raise HEAD (captured INSIDE the policy-blocked ``except`` clause
    # after the sink raised): the agent did not self-commit, so it equals
    # ``fix_start_head``. The policy-blocked rollback anchors against this
    # (review thread ``PRRT_kwDOSJAM6s6Klf78`` / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``
    # (status): report the residue the agent left behind.
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    # ``git restore --source <fix_start_head> --staged --worktree -- <path>``.
    cmd.queue_result(returncode=0)
    # Post-restore status recheck (no more residue after the restore).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <fix_start_head>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
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

    # The policy exception must still propagate so the monitor loop's dedicated
    # handler surfaces ``MONITOR_POLICY_BLOCKED`` semantics — but only AFTER the
    # fix-pass residue has been rolled back.
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

    # The fix-pass MUST roll back to the post-agent/pre-sink HEAD before
    # re-raising so the next monitor attempt does not trip
    # ``PRE_EXISTING_DIRTY_WORKTREE``. The agent did not self-commit here, so
    # ``post_agent_head`` equals ``fix_start_head``.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_cls",
    [
        "ProviderRecoveryRetryError",
        "ProviderRecoveryFallbackError",
        "ProviderRecoveryAuthError",
    ],
)
async def test_pre_push_validation_fix_pass_rolls_back_dirty_residue_before_provider_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: str,
) -> None:
    """Regression for PRRT_kwDOSJAM6s6Kc_Ak (discussion r3433769929).

    When the validation-fix agent leaves protected-scope edits in the
    worktree and ``_commit_dirty_worktree`` raises a provider-recovery
    control-flow exception (``ProviderRecoveryRetryError`` /
    ``ProviderRecoveryFallbackError`` / ``ProviderRecoveryAuthError``)
    from protected-scope repair, the fix-pass must roll back the
    residue to ``fix_start_head`` BEFORE re-raising. Otherwise the
    monitor loop records ``PROVIDER_OUTAGE`` and the next attempt trips
    ``_pre_existing_dirty_repair_worktree_result`` /
    ``PRE_EXISTING_DIRTY_WORKTREE``, wedging the PR.
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
    # Post-raise HEAD (captured INSIDE the provider-recovery ``except``
    # clause after the sink raised): the agent did not self-commit, so it
    # equals ``fix_start_head``. The provider-recovery rollback anchors
    # against this (review thread ``PRRT_kwDOSJAM6s6Klf78`` /
    # ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``
    # (status): report the protected-scope residue the agent left behind.
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    # ``git restore --source <fix_start_head> --staged --worktree -- <path>``.
    cmd.queue_result(returncode=0)
    # Post-restore status recheck (no more residue after the restore).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <fix_start_head>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
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
    # fix-pass residue has been rolled back.
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

    # The fix-pass MUST roll back to the post-agent/pre-sink HEAD before
    # re-raising so the next monitor attempt does not trip
    # ``PRE_EXISTING_DIRTY_WORKTREE``. The agent did not self-commit here, so
    # ``post_agent_head`` equals ``fix_start_head``.
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc_cls", "expected_reason_code"),
    [
        ("ProtectedScopeDiffError", "PROTECTED_SCOPE_DIFF_UNAVAILABLE"),
        ("_MonitorPolicyBlockedError", "MONITOR_POLICY_BLOCKED"),
        (
            "_MonitorAgentRuntimeOwnershipRepairFailedError",
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED",
        ),
        ("_MonitorHeadObjectMissingError", "HEAD_OBJECT_MISSING_UNRECOVERABLE"),
        ("_MonitorMirrorHooksPathRepairFailedError", "MIRROR_HOOKS_PATH_POISONED"),
    ],
)
async def test_pre_push_validation_fix_pass_reason_coded_commit_exception_is_structured_push_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_cls: str,
    expected_reason_code: str,
) -> None:
    """Reason-coded commit exceptions during a fix pass must surface as a
    structured ``_GitPushResult`` (terminal reason code + failure accounting),
    not escape the validated-push boundary and abort the monitor without a
    result.  See review thread PRRT_kwDOSJAM6s6KbbE4.
    """
    from typing import Any

    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.runtime.pr_monitor_runner import types as monitor_types

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "a" * 40
    cmd = FakeCommandRunner()
    # ``_run_pre_push_validation_fix_pass`` reads HEAD before the agent run and
    # again after a clean (no-commit) fix pass.  Both resolve to ``fix_start_head``
    # so the genuine-no-commit rollback path is bypassed when the commit sink
    # raises before reaching the no-commit branch.
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: PYTEST_TEST_FAILURE",
        validation_reason_code="PYTEST_TEST_FAILURE",
        result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _run_pre_push_validation(
        _self: Any,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        """Bypass the real validation flow; the fix-pass commit path is under test."""
        return validation_result

    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation",
        _run_pre_push_validation,
    )

    raised_exc = getattr(monitor_types, exc_cls)(expected_reason_code)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a reason-coded commit-sink failure during a fix pass."""
        raise raised_exc

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == expected_reason_code
    # The terminal reason codes (protected-scope diff unavailable and agent
    # runtime ownership repair failed) must flow into ``terminal_monitor_failure``
    # so the monitor loop records a failed operation and terminates instead of
    # looping forever or crashing the background task.  ``MONITOR_POLICY_BLOCKED``
    # is intentionally non-terminal (the monitor re-polls to re-address, matching
    # the other commit-sink callers in ``fix_cycle.py`` / ``ci_ops.py``).
    if expected_reason_code != "MONITOR_POLICY_BLOCKED":
        assert result.terminal_monitor_failure is True
    # No push should be attempted after a reason-coded fix-pass commit failure.
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_preserves_ignored_paths(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rollback should keep ignored artifacts like .venv while removing generated files."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    cmd = cast(FakeCommandRunner, runner._deps.runner)
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    restore_ref = "d" * 40

    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {restore_ref[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n!! .venv/\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")

    rollback_failure_reason = (
        await pre_push_validation._rollback_failed_pre_push_validation_fix_pass(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            restore_ref=restore_ref,
            pass_number=1,
            reason="test",
        )
    )

    assert rollback_failure_reason is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {restore_ref}" in call for call in joined_calls)
    assert any("clean -ffd -- generated.tmp" in call for call in joined_calls)
    assert all(not ("clean -ffd" in call and ".venv" in call) for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_failure_is_bubbled_as_pre_push_validation_rollback_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed rollback after a fix pass should surface a distinct rollback failure code."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    # Re-read HEAD after a clean (no-commit) fix pass shows HEAD unchanged, so the
    # genuine-no-commit rollback path runs.
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _rollback_failed(*_args: object, **_kwargs: object) -> str:
        """Simulate a rollback failure in fix-pass cleanup."""
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    async def _commit_failed(**_kwargs: object) -> bool:
        """Simulate a repair commit failure exception path."""
        return False

    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_failed)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_post_reset_cleanup_failure_surfaces_cleanup_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful reset plus failed cleanup should not be labeled rollback failed."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    restore_ref = "6" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    # Re-read HEAD after a clean (no-commit) fix pass shows HEAD unchanged, so
    # the genuine-no-commit rollback path runs (not the self-commit path). The
    # commit-sink ``except`` clauses capture HEAD INSIDE each clause (after
    # the sink raised), not before the sink (review thread
    # ``PRRT_kwDOSJAM6s6Klf78`` / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {restore_ref[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? validation-artifact.log\n")
    cmd.queue_result(returncode=1, stderr="clean failed")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _commit_failed(**_kwargs: object) -> bool:
        """Simulate a repair commit failure after the agent attempted a fix."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_failed)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "rollback failed" not in result.stderr
    assert "cleanup failed" in result.stderr
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_does_not_clean_when_reset_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed rollback reset should preserve untracked files for manual recovery."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    cmd = cast(FakeCommandRunner, runner._deps.runner)
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    restore_ref = "b" * 40
    cmd.queue_result(returncode=1, stdout="")

    rollback_failure_reason = (
        await pre_push_validation._rollback_failed_pre_push_validation_fix_pass(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            restore_ref=restore_ref,
            pass_number=1,
            reason="reset_failed",
        )
    )

    assert rollback_failure_reason == "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {restore_ref}" in call for call in joined_calls)
    assert not any("clean -ffd" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_revalidates_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair passes should re-run validation before allowing push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "b" * 40
    fixed_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    # The commit sink then advances HEAD to ``fixed_head``. The commit-sink
    # ``except`` clauses capture HEAD INSIDE each clause (after the sink
    # raised), not before the sink (review thread ``PRRT_kwDOSJAM6s6Klf78``
    # / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    # merge-base --is-ancestor: the dirty commit still descends from fix_start_head.
    cmd.queue_result(returncode=0)
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
        """Record a synthetic commit and return a successful outcome."""
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
async def test_pre_push_validation_fix_prompt_includes_underlying_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix prompts should include the first failing validation reason code."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "d" * 40
    fixed_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    # The commit sink then advances HEAD to ``fixed_head``. The commit-sink
    # ``except`` clauses capture HEAD INSIDE each clause (after the sink
    # raised), not before the sink (review thread ``PRRT_kwDOSJAM6s6Klf78``
    # / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    # merge-base --is-ancestor: the dirty commit still descends from fix_start_head.
    cmd.queue_result(returncode=0)
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
        _validation_result(
            tmp_path,
            ok=False,
            reason_code="PYTEST_TEST_FAILURE",
        ),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[str] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record a synthetic commit and return a successful outcome."""
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
    assert "Reason code: PYTEST_TEST_FAILURE" in adapter.calls[0]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commits_agent_failure_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero fix agents should preserve evidence and still commit attempted fixes."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "f" * 40
    fixed_head = "1" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    # The commit sink then advances HEAD to ``fixed_head``. The commit-sink
    # ``except`` clauses capture HEAD INSIDE each clause (after the sink
    # raised), not before the sink (review thread ``PRRT_kwDOSJAM6s6Klf78``
    # / ``PRRT_kwDOSJAM6s6KpAD6``).
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    # merge-base --is-ancestor: the dirty commit still descends from fix_start_head.
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="agent stdout", stderr="agent stderr", returncode=2)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[dict[str, object]] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record the attempted fix commit and report success."""
        committed.append(kwargs)
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
    assert result.pushed is True
    assert len(adapter.calls) == 1
    assert committed[0]["message"] == f"awf: pre-push validation fix for {workspace_id}"
    assert "agent stdout" in "\n".join(committed[0]["command_evidence"])  # type: ignore[index]
    assert "agent stderr" in "\n".join(committed[0]["command_evidence"])  # type: ignore[index]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_cleanup_failure_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Fix-pass cleanup failures should surface as fix failures and avoid push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'2' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_pre_push_fix_cleanup",
            source="agent",
            label="monitor-pre-push-validation-fix",
            message="tagged process still running",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert "fix pass failed" in result.stderr
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commit_fail_returns_fix_failed_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed fix commit attempts should surface ``PRE_PUSH_VALIDATION_FIX_FAILED``."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    # Post-agent/pre-sink HEAD (``post_agent_head``): the agent did not
    # self-commit, so it equals the fix-start HEAD (review thread
    # ``PRRT_kwDOSJAM6s6Klf78``).
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'f' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _no_commit(**_kwargs: object) -> bool:
        """Return a failed commit result for the fix-pass test."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _no_commit)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"
    assert result.details["failing_command"] == "pytest -q"
    assert result.details["failing_returncode"] == 1
    assert "fix pass failed" in result.stderr


@pytest.mark.unit
async def test_disallowed_fix_passes_skip_fix_pass_on_failed_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``allow_validation_fix_passes=False`` returns the validation failure unchanged.

    The operator-hint resume path disables fix passes while an approve-and-keep grant
    is still active: a fix pass commits through ``_commit_dirty_worktree``, whose
    protected-scope check honors the STILL-ACTIVE grant, so it could publish new edits
    to the granted protected path under an approval meant only for the preserved commit
    (PR #609 comment 4512881681). When disabled, a failing validation must NOT invoke a
    fix pass and must surface ``PRE_PUSH_VALIDATION_FAILED`` (the grant survives for a
    re-resume).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]

    async def _fix_pass_must_not_run(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        """Fail the test if a fix pass is invoked while fix passes are disabled."""
        pytest.fail("fix passes must be skipped when allow_validation_fix_passes=False")

    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _fix_pass_must_not_run,
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        allow_validation_fix_passes=False,
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FAILED"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_validates_protected_scope_after_missing_head_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered missing-HEAD commit must still pass protected-scope validation.

    Regression for PRRT_kwDOSJAM6s6KyPln: the fix-pass-specific missing-HEAD
    recovery can create a commit from filesystem state before the normal dirty
    commit sink runs. If that recovered commit leaves the worktree clean, the
    sink returns False. Re-check the committed
    ``fix_start_head..recovered`` delta before accepting the self-committed pass.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass_module

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    recovered_head = "2" * 40
    cmd = FakeCommandRunner()
    # Recovered protected-scope delta: ``git diff --name-only -z
    # fix_start_head..recovered``.
    cmd.queue_result(returncode=0, stdout=".github/workflows/ci.yml\0")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation and recovered HEAD\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results = [fix_start_head, recovered_head]
    recovered_validation_calls: list[dict[str, object]] = []

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return recovered_head

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Clean worktree after recovery: without the fix, protected scope is skipped."""
        return False

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[object]:
        recovered_validation_calls.append({"args": args, **kwargs})
        return []

    async def _repair_protected_scope_changes_before_commit(
        **_kwargs: object,
    ) -> CommandResult:
        raise AssertionError("committed recovery must not use synthetic dirty status repair")

    async def _head_descends_from(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _cleanup_committed_pre_push_validation_fix_pass(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        fix_pass_module,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(pre_push_validation, "_head_descends_from", _head_descends_from)
    monkeypatch.setattr(
        pre_push_validation,
        "_cleanup_committed_pre_push_validation_fix_pass",
        _cleanup_committed_pre_push_validation_fix_pass,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
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

    assert committed is True
    assert cleanup_failure_reason is None
    assert recovered_validation_calls == [
        {
            "args": (runner,),
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "base_ref": fix_start_head,
            "changed_paths": [".github/workflows/ci.yml"],
        }
    ]
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_blocks_recovered_commit_protected_scope_violations(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered commits must use committed protected diffs, not HEAD/worktree diffs.

    Regression for PRRT_kwDOSJAM6s6Ky-rn: after missing-HEAD recovery commits the
    filesystem tree, a dirty-status repair check compares the recovered
    ``HEAD:path`` with the recovered worktree and can miss protected workflow
    weakenings. The committed ``fix_start_head..recovered`` classifier must catch
    the violation and roll back before validation can continue to push.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass_module

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "3" * 40
    recovered_head = "4" * 40
    old_workflow = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
"""
    new_workflow = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=".github/workflows/ci.yml\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=old_workflow)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=new_workflow)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation and recovered HEAD\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results = [fix_start_head]
    rollback_calls: list[dict[str, object]] = []

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return recovered_head

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("protected recovered commit must block before dirty commit sink")

    async def _repair_protected_scope_changes_before_commit(
        **_kwargs: object,
    ) -> CommandResult:
        raise AssertionError("committed recovery must not use synthetic dirty status repair")

    async def _rollback_failed_pre_push_validation_fix_pass(
        *_args: object,
        **kwargs: object,
    ) -> str | None:
        rollback_calls.append(dict(kwargs))
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        fix_pass_module,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_pre_push_validation_fix_pass,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
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

    assert committed is False
    assert cleanup_failure_reason == "PROTECTED_SCOPE_REPAIR_FAILED"
    assert rollback_calls == [
        {
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "restore_ref": fix_start_head,
            "pass_number": 1,
            "reason": "recovered_protected_scope_repair_failed",
        }
    ]
    assert rev_parse_results == []
