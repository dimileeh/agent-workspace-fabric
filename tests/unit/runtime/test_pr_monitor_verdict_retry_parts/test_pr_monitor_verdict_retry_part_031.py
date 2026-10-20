"""An untyped error after a successful rollback parks the item (#1076, part 031).

Five sites (S1-S5) used to roll the item back and then re-raise the original
error. An untyped one reached nothing that handles it, so the batch skipped its
push and the workspace failed. Now the item parks ``needs_human`` with
``AGENT_VERDICT_ITEM_UNEXPECTED_ERROR``. A failed rollback, a cause with a
dedicated upstream handler and cancellation behave exactly as before.

Each arming helper injects the failure at one site. The site label asserted in
the parked reason proves the injection hit the intended site.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import structlog

from awf.adapters.base import AgentRunResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_ITEM_UNEXPECTED_ERROR,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
    remember_item_start_head,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner
from tests.unit.runtime._verdict_trailing_output_fixtures import F3

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_CORRECTION_HEAD = "c" * 40
_PRESERVED_HEAD = "d" * 40
_PARKED_EVENT = "monitor.agent_verdict_item_unexpected_error_parked"
_SITES = [
    "agent_invocation",
    "commit_sink",
    "post_attempt_tip_probe",
    "correction_end_head_probe",
    "unscoped_fix_evidence_probe",
]
_UNTYPED = [OSError, RuntimeError, TimeoutError]


@dataclass
class _Armed:
    runner: _VerdictRunner
    invoke: Callable[[], Awaitable[VerdictResult]]
    floor: str


_Arm = Callable[[Path, pytest.MonkeyPatch, BaseException, bool], _Armed]


def _runner(
    tmp_path: Path,
    *,
    outputs: list[str],
    heads: list[str],
    dirty: list[bool] | None = None,
    reset_fails: bool = False,
) -> _VerdictRunner:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=list(outputs),
        heads_after_attempt=heads,
        dirty_after_attempt=dirty,
        reset_fails=reset_fails,
    )
    runner.current_head = _ITEM_START_HEAD
    return runner


def _arm_agent_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, reset_fails: bool
) -> _Armed:
    """S1: the agent self-commits, then the invocation raises."""
    del monkeypatch
    runner = _runner(tmp_path, outputs=["unused"], heads=[_ATTEMPT0_HEAD], reset_fails=reset_fails)

    async def _agent(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.current_head = _ATTEMPT0_HEAD
        raise error

    runner._run_monitor_agent_with_service_recovery = _agent
    return _Armed(runner, lambda: _invoke(runner), _ITEM_START_HEAD)


def _arm_commit_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, reset_fails: bool
) -> _Armed:
    """S2: the commit sink advances HEAD, then raises (part_003 shape)."""
    del monkeypatch
    runner = _runner(
        tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads=[_ATTEMPT0_HEAD],
        dirty=[True],
        reset_fails=reset_fails,
    )

    async def _sink(**_kwargs: object) -> bool:
        runner.current_head = _ATTEMPT0_HEAD
        raise error

    runner._commit_dirty_worktree = _sink
    return _Armed(runner, lambda: _invoke(runner), _ITEM_START_HEAD)


def _arm_post_attempt_tip_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, reset_fails: bool
) -> _Armed:
    """S3: the 3rd HEAD read (post-attempt tip) raises (part_006 sequence)."""
    del monkeypatch
    runner = _runner(
        tmp_path,
        outputs=["malformed after editing"],
        heads=[_ATTEMPT0_HEAD],
        dirty=[True],
        reset_fails=reset_fails,
    )
    calls = 0

    async def _rev_parse(_path: Path) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _ITEM_START_HEAD
        if calls == 2:
            runner.current_head = _ATTEMPT0_HEAD
            return _ATTEMPT0_HEAD
        if calls == 3:
            raise error
        return runner.current_head

    runner._rev_parse_head = _rev_parse
    return _Armed(runner, lambda: _invoke(runner), _ITEM_START_HEAD)


def _arm_correction_end_head_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, reset_fails: bool
) -> _Armed:
    """S4: the 7th HEAD read (correction end) raises (part_006 sequence)."""
    del monkeypatch
    runner = _runner(
        tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads=[_ATTEMPT0_HEAD, _CORRECTION_HEAD],
        dirty=[True, False],
        reset_fails=reset_fails,
    )
    calls = 0

    async def _rev_parse(_path: Path) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _ITEM_START_HEAD
        if calls == 2:
            runner.current_head = _ATTEMPT0_HEAD
            return _ATTEMPT0_HEAD
        if calls in (3, 4):
            return _ATTEMPT0_HEAD
        if calls in (5, 6):
            return _CORRECTION_HEAD
        if calls == 7:
            raise error
        return runner.current_head

    runner._rev_parse_head = _rev_parse
    return _Armed(runner, lambda: _invoke(runner), _ITEM_START_HEAD)


def _arm_unscoped_fix_evidence_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, reset_fails: bool
) -> _Armed:
    """S5 via ``park_unaccepted_correction``: F3 twice after a contentful commit (part_030)."""
    runner = _runner(
        tmp_path,
        outputs=[F3, F3],
        heads=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty=[True, False],
        reset_fails=reset_fails,
    )
    real_evidence = comment_verdict._item_fix_evidence

    async def _probe(r: object, **kwargs: object) -> bool:
        if kwargs.get("item_path") is None:
            raise error
        return await real_evidence(r, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _probe)

    async def _invoke_item() -> VerdictResult:
        return await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=_ITEM_START_HEAD,
            evidence_item_path="src/app.py",
            evidence_item_line=10,
        )

    return _Armed(runner, _invoke_item, _ITEM_START_HEAD)


_ARM: dict[str, _Arm] = {
    "agent_invocation": _arm_agent_invocation,
    "commit_sink": _arm_commit_sink,
    "post_attempt_tip_probe": _arm_post_attempt_tip_probe,
    "correction_end_head_probe": _arm_correction_end_head_probe,
    "unscoped_fix_evidence_probe": _arm_unscoped_fix_evidence_probe,
}


def _events(captured: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [entry for entry in captured if entry.get("event") == event]


@pytest.mark.unit
@pytest.mark.parametrize("site", _SITES)
@pytest.mark.parametrize("error_type", _UNTYPED, ids=lambda t: t.__name__)
async def test_untyped_error_after_successful_rollback_parks_the_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    error_type: type[Exception],
) -> None:
    """Acceptance 1: parked, rolled back to the attempt floor, warning emitted."""
    error = error_type(f"{site} failed")
    armed = _ARM[site](tmp_path, monkeypatch, error, False)

    with structlog.testing.capture_logs() as captured:
        result = await armed.invoke()

    assert result.verdict == "needs_human"
    assert result.kept_commit is False
    assert result.reason is not None
    assert result.reason.startswith(
        f"{AGENT_VERDICT_ITEM_UNEXPECTED_ERROR}: {error_type.__name__} at {site} "
    )
    assert armed.floor[:12] in result.reason
    assert f"{site} failed" in result.reason
    assert armed.runner.reset_targets == [armed.floor]
    assert armed.runner.current_head == armed.floor
    parked = _events(captured, _PARKED_EVENT)
    assert len(parked) == 1
    assert parked[0]["log_level"] == "warning"
    assert parked[0]["reason_code"] == AGENT_VERDICT_ITEM_UNEXPECTED_ERROR
    assert parked[0]["exc_type"] == error_type.__name__
    assert parked[0]["site"] == site


@pytest.mark.unit
async def test_untyped_error_without_a_git_config_baseline_stays_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6uXI6H: snapshot failure must not park after a no-op restore.

    Remember warns and the agent still runs. Rollback then reports success
    because nothing was snapshotted. Continuing would keep agent-changed Git
    settings for the next item and the push, so the item stays terminal.
    """

    def _snapshot_fails(_worktree_path: Path) -> bool:
        return False

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot_fails)
    error = RuntimeError("agent transport dropped")
    armed = _arm_agent_invocation(tmp_path, monkeypatch, error, False)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await armed.invoke()

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert caught.value.__cause__ is error
    assert "Git config baseline" in str(caught.value)
    assert armed.runner.reset_targets == [armed.floor]
    assert _events(captured, _PARKED_EVENT) == []


@pytest.mark.unit
@pytest.mark.parametrize("site", _SITES)
@pytest.mark.parametrize("error_type", _UNTYPED, ids=lambda t: t.__name__)
async def test_untyped_error_with_failed_rollback_stays_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    error_type: type[Exception],
) -> None:
    """Acceptance 2: the worktree state is unknown, so the typed terminal error stays."""
    error = error_type(f"{site} failed")
    armed = _ARM[site](tmp_path, monkeypatch, error, True)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await armed.invoke()

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert caught.value.__cause__ is error
    assert armed.runner.reset_targets == [armed.floor]
    assert _events(captured, _PARKED_EVENT) == []


def _dedicated_error(kind: str) -> Exception:
    if kind == "head_object_missing":
        return _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_UNRECOVERABLE", "head object missing"
        )
    return ProtectedScopeDiffError("protected-scope diff unavailable")


@pytest.mark.unit
@pytest.mark.parametrize("site", _SITES)
@pytest.mark.parametrize("kind", ["head_object_missing", "protected_scope_diff"])
async def test_dedicated_error_still_propagates_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    kind: str,
) -> None:
    """Acceptance 3: a cause with its own upstream handler keeps reaching it."""
    error = _dedicated_error(kind)
    armed = _ARM[site](tmp_path, monkeypatch, error, False)

    with structlog.testing.capture_logs() as captured, pytest.raises(type(error)) as caught:
        await armed.invoke()

    assert caught.value is error
    assert armed.runner.reset_targets == [armed.floor]
    assert _events(captured, _PARKED_EVENT) == []


@pytest.mark.unit
async def test_policy_block_reaching_the_invocation_handler_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3, S1: a dedicated class that only S1's ``except Exception`` sees."""
    error = _MonitorPolicyBlockedError("blocked by policy")
    armed = _arm_agent_invocation(tmp_path, monkeypatch, error, False)

    with pytest.raises(_MonitorPolicyBlockedError) as caught:
        await armed.invoke()

    assert caught.value is error
    assert armed.runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_compose_cleanup_error_from_the_commit_sink_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3, S2: ``ComposeExecCleanupError`` reaches S2's ``except Exception`` arm."""
    error = ComposeExecCleanupError(
        invocation_id="sink", source="recovery", label="pre-commit", message="cleanup failed"
    )
    armed = _arm_commit_sink(tmp_path, monkeypatch, error, False)

    with pytest.raises(ComposeExecCleanupError) as caught:
        await armed.invoke()

    assert caught.value is error
    assert armed.runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_post_rollback_mirror_hook_repair_failure_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3, S1: the hook repair after the rollback raises its own terminal error."""
    armed = _arm_agent_invocation(tmp_path, monkeypatch, RuntimeError("transport dropped"), False)
    monkeypatch.setattr(
        comment_verdict, "mirror_path_for_worktree", lambda _path: tmp_path / "mirror.git"
    )

    async def _repair(*, workspace_id: str, mirror_path: Path, stage: str) -> None:
        del workspace_id, mirror_path
        if stage == "after_comment_agent_exception":
            raise _MonitorMirrorHooksPathRepairFailedError()

    monkeypatch.setattr(comment_verdict, "_repair_mirror_hooks_or_raise", _repair)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(_MonitorMirrorHooksPathRepairFailedError),
    ):
        await armed.invoke()

    assert armed.runner.reset_targets == [_ITEM_START_HEAD]
    assert _events(captured, _PARKED_EVENT) == []


@pytest.mark.unit
@pytest.mark.parametrize("site", _SITES)
async def test_cancellation_still_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    """Acceptance 4: ``CancelledError`` bypasses every site and is never parked."""
    armed = _ARM[site](tmp_path, monkeypatch, asyncio.CancelledError(), False)

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await armed.invoke()

    assert armed.runner.reset_targets == [armed.floor]
    assert _events(captured, _PARKED_EVENT) == []


@pytest.mark.unit
async def test_unknown_floor_keeps_raising_the_cause(tmp_path: Path) -> None:
    """No readable start HEAD at all: the "successful" rollback ran nothing, so no park."""
    (tmp_path / "ws_protocol").mkdir()
    error = RuntimeError("agent transport dropped")
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["unused"],
        heads_after_attempt=[_ATTEMPT0_HEAD],
        rev_parse_sequence=[None, None, None, None],
    )

    async def _agent(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        raise error

    runner._run_monitor_agent_with_service_recovery = _agent

    with pytest.raises(RuntimeError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
        )

    assert caught.value is error
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_missing_worktree_keeps_raising_the_cause(tmp_path: Path) -> None:
    """No local worktree: nothing was rolled back, so the cause propagates unchanged."""
    error = RuntimeError("unexpected commit sink failure")
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[_ATTEMPT0_HEAD],
        dirty_after_attempt=[True],
    )

    async def _sink(**_kwargs: object) -> bool:
        raise error

    runner._commit_dirty_worktree = _sink

    with pytest.raises(RuntimeError) as caught:
        await _invoke(runner)

    assert caught.value is error
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_parked_reason_is_redacted_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git error can echo a credentialed URL and be arbitrarily long."""
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    error = OSError(f"fetch https://user:{secret}@example.invalid/r.git failed " + "y" * 2000)
    armed = _arm_commit_sink(tmp_path, monkeypatch, error, False)

    with structlog.testing.capture_logs() as captured:
        result = await armed.invoke()

    assert result.reason is not None
    assert secret not in result.reason
    assert len(result.reason) <= 500
    assert secret not in str(_events(captured, _PARKED_EVENT)[0]["error"])


@pytest.mark.unit
async def test_park_consumes_the_preserved_timeout_anchor(tmp_path: Path) -> None:
    """A park is a verdict: the #932 anchor is not re-armed (accepted, as for #1075 parks).

    Re-attempt after a preserved timeout (part_013 shape): the rollback floor is
    the preserved HEAD, so the preserved commits survive the park too.
    """
    item_id = "PRRT_anchor"
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[_CORRECTION_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD

    async def _sink(**_kwargs: object) -> bool:
        runner.current_head = _CORRECTION_HEAD
        raise RuntimeError("unexpected commit sink failure")

    runner._commit_dirty_worktree = _sink
    state = MonitorState()
    remember_item_start_head(state, item_id, _ITEM_START_HEAD)

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {item_id}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_PRESERVED_HEAD,
        evidence_item_id=item_id,
    )

    assert result.verdict == "needs_human"
    assert _PRESERVED_HEAD[:12] in (result.reason or "")
    assert runner.reset_targets == [_PRESERVED_HEAD]
    assert runner.current_head == _PRESERVED_HEAD
    assert item_start_head_state_key(item_id) not in state.threads_addressed_ids
