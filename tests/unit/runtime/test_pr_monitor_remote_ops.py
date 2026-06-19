"""Regression tests for PR monitor push outcome classification."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner import pre_push_validation, remote_ops
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _MONITOR_POLICY_BLOCKED_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    VALIDATION_WORKTREE_CLEANUP_FAILED as REMOTE_OPS_VALIDATION_WORKTREE_CLEANUP_FAILED,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY as REMOTE_OPS_VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    VALIDATION_WORKTREE_STATUS_FAILED as REMOTE_OPS_VALIDATION_WORKTREE_STATUS_FAILED,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _append_git_recovery_failure,
    _git_failure_message,
    _git_push_failure_outcome,
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
)


def _make_push_result(reason_code: str) -> _GitPushResult:
    """Build a failure push-result payload for push-outcome mapping tests."""
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        reason_code=reason_code,
    )


@pytest.mark.parametrize(
    "reason_code",
    [
        "PRE_PUSH_VALIDATION_FAILED",
        "PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED",
        "PRE_PUSH_VALIDATION_FIX_FAILED",
        "PRE_PUSH_VALIDATION_ROLLBACK_FAILED",
        "PRE_PUSH_VALIDATION_REPARENT_FAILED",
    ],
)
@pytest.mark.unit
def test_git_push_failure_outcome_maps_pre_push_validation_reasons(reason_code: str) -> None:
    """Pre-push validation reason codes should map to the monitor failure outcome."""
    assert _git_push_failure_outcome(_make_push_result(reason_code)) == "pre_push_validation_failed"


@pytest.mark.unit
def test_git_push_failure_outcome_maps_toolchain_missing_separately() -> None:
    assert (
        _git_push_failure_outcome(_make_push_result("PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"))
        == "pre_push_validation_toolchain_missing"
    )


@pytest.mark.unit
def test_git_push_terminal_monitor_failure_maps_rollback_failed_as_terminal() -> None:
    """Roll-back failure on pre-push validation should remain terminal."""
    assert _make_push_result("PRE_PUSH_VALIDATION_ROLLBACK_FAILED").terminal_monitor_failure is True


@pytest.mark.unit
def test_git_push_terminal_monitor_failure_does_not_treat_blocked_pause_as_terminal() -> None:
    """A protected-scope pause preserves the workspace for operator action."""
    result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
        paused_into_blocked=True,
    )

    assert result.protected_scope_blocked is True
    assert result.terminal_monitor_failure is False


@pytest.mark.unit
def test_git_push_terminal_monitor_failure_maps_reparent_failed_as_terminal() -> None:
    """Re-parent failure leaves HEAD on a non-descendant commit with no rollback, so it
    must end monitor recovery rather than let a later iteration push the orphaning HEAD
    and mask the error as a non-fast-forward (PR #422 thread)."""
    assert _make_push_result("PRE_PUSH_VALIDATION_REPARENT_FAILED").terminal_monitor_failure is True


@pytest.mark.parametrize(
    "reason_code",
    [
        _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        _MIRROR_HOOKS_PATH_POISONED_REASON,
    ],
)
@pytest.mark.unit
def test_git_push_terminal_monitor_failure_maps_unrecoverable_git_repairs_as_terminal(
    reason_code: str,
) -> None:
    """Unrecoverable local git repairs should fail the monitor instead of retrying."""
    assert _make_push_result(reason_code).terminal_monitor_failure is True


@pytest.mark.unit
def test_git_push_failure_outcome_defaults_to_git_push_failed() -> None:
    """Unknown push failures should retain the default push-failed outcome."""
    assert _git_push_failure_outcome(_make_push_result("UNKNOWN_FAILURE")) == "git_push_failed"


@pytest.mark.unit
def test_remote_ops_worktree_constants_match_validation_worktree() -> None:
    """Remote-op worktree reason codes should remain aligned with canonical constants."""
    assert REMOTE_OPS_VALIDATION_WORKTREE_CLEANUP_FAILED == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert (
        REMOTE_OPS_VALIDATION_WORKTREE_PRE_EXISTING_DIRTY == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    assert REMOTE_OPS_VALIDATION_WORKTREE_STATUS_FAILED == VALIDATION_WORKTREE_STATUS_FAILED
    assert (
        pre_push_validation.VALIDATION_WORKTREE_CLEANUP_FAILED == VALIDATION_WORKTREE_CLEANUP_FAILED
    )
    assert (
        pre_push_validation.VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
        == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    )
    assert (
        pre_push_validation.VALIDATION_WORKTREE_STATUS_FAILED == VALIDATION_WORKTREE_STATUS_FAILED
    )


@pytest.mark.unit
async def test_rev_parse_head_strips_git_object_lookup_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD anchors must not resolve through inherited private git object stores."""
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/alternate-objects")
    monkeypatch.setenv("AWF_REV_PARSE_ENV_SENTINEL", "kept")

    class _FakeCommandRunner:
        def __init__(self) -> None:
            self.env: Mapping[str, str] | None = None

        async def run(
            self,
            _args: list[str],
            *,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            self.env = env
            return CommandResult(returncode=0, stdout=f"{'a' * 40}\n", stderr="")

    command_runner = _FakeCommandRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    result = await remote_ops._rev_parse_head(runner, tmp_path)

    assert result == "a" * 40
    assert command_runner.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in command_runner.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in command_runner.env
    assert command_runner.env["AWF_REV_PARSE_ENV_SENTINEL"] == "kept"


@pytest.mark.unit
def test_git_push_failure_outcome_maps_repair_and_protected_scope_reasons() -> None:
    """Monitor push-repair and workflow-scope outcomes map to their specific buckets."""
    assert (
        _git_push_failure_outcome(
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            )
        )
        == "protected_scope_diff_unavailable"
    )
    assert (
        _git_push_failure_outcome(
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            )
        )
        == "protected_scope_push_blocked"
    )
    assert _git_push_failure_outcome(_make_push_result("REPAIR_WORKTREE_STATUS_FAILED")) == (
        "repair_start_blocked"
    )


@pytest.mark.unit
def test_monitor_policy_blocked_error_preserves_specific_reason_code() -> None:
    """Policy exceptions default to monitor-policy but can carry protected-scope reasons."""
    default_error = _MonitorPolicyBlockedError("supply-chain blocked")
    protected_error = _MonitorPolicyBlockedError(
        "protected scope blocked",
        reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
    )
    protected_result = _make_push_result(protected_error.reason_code)

    assert default_error.reason_code == _MONITOR_POLICY_BLOCKED_REASON
    assert protected_result.protected_scope_blocked is True
    assert protected_result.terminal_monitor_failure is True
    assert _git_push_failure_outcome(protected_result) == "protected_scope_push_blocked"


@pytest.mark.unit
def test_git_push_terminal_monitor_failure_maps_recovered_protected_scope_repair_failure() -> None:
    """Recovered protected-scope repair failures must stop monitor retry.

    Missing-HEAD recovery can leave HEAD on a recovered commit that still
    contains protected-scope changes. That pre-push validation failure must stay
    on the protected-scope terminal path instead of retrying against the same
    recovered commit.
    """
    result = _make_push_result(_PROTECTED_SCOPE_REPAIR_FAILED_REASON)

    assert result.protected_scope_blocked is True
    assert result.terminal_monitor_failure is True
    assert _git_push_failure_outcome(result) == "protected_scope_push_blocked"


@pytest.mark.unit
def test_git_failure_message_prefers_stderr_then_stdout() -> None:
    """Failure messages should prefer stderr over stdout when formatting command output."""
    assert (
        _git_failure_message(
            "git push",
            CommandResult(returncode=128, stdout="", stderr=" denied \n"),
        )
        == "git push failed with exit code 128; stderr: denied"
    )
    assert (
        _git_failure_message(
            "git fetch",
            CommandResult(returncode=1, stdout=" retried \n", stderr=""),
        )
        == "git fetch failed with exit code 1; stdout: retried"
    )
    assert (
        _git_failure_message(
            "git status",
            CommandResult(returncode=2, stdout="", stderr=""),
        )
        == "git status failed with exit code 2"
    )


@pytest.mark.unit
def test_append_git_recovery_failure_includes_available_context() -> None:
    """Recovery failure messages should include upstream and fallback operation context."""
    assert _append_git_recovery_failure(
        push_stderr="push rejected",
        recovery_stderr="fetch failed",
        operation="fetch",
    ) == (
        "push rejected\n"
        "AWF worktree recovery failed during git push failure resync "
        "(fetch failed: fetch failed)"
    )
    assert (
        _append_git_recovery_failure(
            push_stderr=None,
            recovery_stderr=None,
            operation="reset",
        )
        == "AWF worktree recovery failed during git push failure resync (reset failed)"
    )


@pytest.mark.unit
def test_git_push_terminal_monitor_failure_maps_dirty_finalize_unowned_delta_as_terminal() -> None:
    """Pre-push dirty finalize that commits an unowned path must end monitor recovery.

    The pre-push dirty finalize re-validates the operation delta after the
    commit sink's side effects and fails closed with
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` when a path outside the owned
    delta was committed (review thread ``PRRT_kwDOSJAM6s6KZP8f``). A bad local
    commit may already exist at that point, so the monitor loop must stop
    iterating instead of retrying and risk pushing the unowned commit on a
    later iteration (regression for review thread ``PRRT_kwDOSJAM6s6KZ33M``).
    """
    assert (
        _make_push_result(_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON).terminal_monitor_failure
        is True
    )


@pytest.mark.unit
def test_git_push_failure_outcome_maps_dirty_finalize_unowned_delta() -> None:
    """The dirty finalize unowned-delta reason should classify as a pre-push validation failure.

    It is a pre-push validation reason code (returned by
    ``_run_pre_push_validation`` via ``_pre_push_dirty_result``), so it must map
    to ``pre_push_validation_failed`` like the other finalize dirty reasons
    (regression for review thread ``PRRT_kwDOSJAM6s6KZ33M``).
    """
    assert (
        _git_push_failure_outcome(_make_push_result(_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON))
        == "pre_push_validation_failed"
    )
