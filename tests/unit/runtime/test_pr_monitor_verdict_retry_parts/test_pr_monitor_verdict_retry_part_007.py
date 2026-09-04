"""Rollback-failure branches around the compose-cleanup commit sink and end-head probes."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_NON_FIXED_WITH_MUTATION,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorHeadObjectMissingError,
    _MonitorPolicyBlockedError,
)
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_CLEANUP_ERROR = ComposeExecCleanupError(
    invocation_id="cleanup-failed",
    source="recovery",
    label="agent",
    message="cleanup failed",
)


def _cleanup_runner(tmp_path: Path) -> _VerdictRunner:
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=["b" * 40],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        raise _CLEANUP_ERROR

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup
    return runner


def _fail_rollback_after(monkeypatch: pytest.MonkeyPatch, successes: int) -> list[int]:
    """Let the first ``successes`` rollbacks succeed, then fail every later one."""
    calls: list[int] = []
    real = comment_verdict._rollback_unaccepted_protocol_retry_changes

    async def _rollback(*args: object, **kwargs: object) -> bool:
        calls.append(len(calls))
        if len(calls) <= successes:
            return await real(*args, **kwargs)  # type: ignore[arg-type]
        return False

    monkeypatch.setattr(comment_verdict, "_rollback_unaccepted_protocol_retry_changes", _rollback)
    return calls


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sink_error", "expected"),
    [
        (_MonitorPolicyBlockedError("POLICY_BLOCKED"), _MonitorPolicyBlockedError),
        (_MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING"), _MonitorHeadObjectMissingError),
    ],
)
async def test_compose_cleanup_sink_infrastructure_exit_rollback_failure_keeps_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sink_error: Exception,
    expected: type[Exception],
) -> None:
    """Reason-coded sink exits propagate even when the post-sink rollback fails."""
    runner = _cleanup_runner(tmp_path)

    async def _sink_raises(**_kwargs: object) -> bool:
        raise sink_error

    runner._commit_dirty_worktree = _sink_raises
    _fail_rollback_after(monkeypatch, successes=1)
    with pytest.raises(expected):
        await _invoke(runner)


@pytest.mark.unit
async def test_compose_cleanup_sink_recovery_exit_rollback_failure_is_protocol_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError

    runner = _cleanup_runner(tmp_path)

    async def _sink_raises(**_kwargs: object) -> bool:
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _sink_raises
    _fail_rollback_after(monkeypatch, successes=1)
    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)
    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "commit sink infrastructure exit" in str(caught.value)


@pytest.mark.unit
async def test_compose_cleanup_sink_unexpected_failure_rollback_failure_is_protocol_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _cleanup_runner(tmp_path)

    async def _sink_raises(**_kwargs: object) -> bool:
        raise RuntimeError("session gone")

    runner._commit_dirty_worktree = _sink_raises
    _fail_rollback_after(monkeypatch, successes=1)
    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)
    assert "unexpected compose cleanup commit sink failure" in str(caught.value)


@pytest.mark.unit
async def test_compose_cleanup_post_sink_rollback_failure_is_protocol_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _cleanup_runner(tmp_path)

    async def _sink_ok(**_kwargs: object) -> bool:
        return True

    runner._commit_dirty_worktree = _sink_ok
    calls = _fail_rollback_after(monkeypatch, successes=1)
    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)
    assert caught.value.__cause__ is _CLEANUP_ERROR
    assert "after compose cleanup commit sink." in str(caught.value)
    assert len(calls) == 2


@pytest.mark.unit
async def test_correction_end_head_unreadable_rollback_failure_is_protocol_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable correction-end HEAD plus failed rollback surfaces the rollback failure."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed", "AWF-VERDICT: FALSE POSITIVE: nothing to fix"],
        heads_after_attempt=[item_start_head, item_start_head],
        # attempt-0 start, attempt-0 evidence, post-attempt tip, correction start,
        # pre-sink, correction evidence, then the correction-end probe fails.
        rev_parse_sequence=[item_start_head] * 6 + [None],
    )
    _fail_rollback_after(monkeypatch, successes=0)
    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)
    assert "unreadable end HEAD" in str(caught.value)


@pytest.mark.unit
async def test_pre_sink_probe_failure_chains_into_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pre-sink probe is the cause of a later mutation rollback failure."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed", "AWF-VERDICT: FALSE POSITIVE: nothing to fix"],
        heads_after_attempt=[item_start_head, "c" * 40],
        dirty_after_attempt=[False, True],
    )
    probe_error = OSError("pre-sink git spawn failed")
    reads = {"n": 0}

    async def _rev_parse_head(_path: Path) -> str | None:
        reads["n"] += 1
        # attempt-0 start, attempt-0 evidence, post-attempt tip, correction start,
        # then the pre-sink probe fails.
        if reads["n"] == 5:
            raise probe_error
        return runner.current_head

    runner._rev_parse_head = _rev_parse_head

    async def _rollback_fails(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(comment_verdict, "_rollback_or_classify_failure", _rollback_fails)
    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)
    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert caught.value.__cause__ is probe_error
    assert caught.value.reason_code != AGENT_NON_FIXED_WITH_MUTATION
