"""Terminal monitor behavior after the bounded verdict retry is exhausted."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog.testing

from awf.common.commands import CommandResult
from awf.common.github_client import RepoRef
from awf.db.enums import FailureReason
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner import fix_cycle
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult


@pytest.mark.unit
async def test_fix_cycle_turns_exhausted_protocol_retry_into_terminal_agent_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_protocol_terminal"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    forge_calls: list[str] = []

    async def _none(**_kwargs: object) -> None:
        return None

    async def _no_abandoned_repairs(**kwargs: object) -> tuple[str, None]:
        return str(kwargs["local_head"]), None

    async def _start(**_kwargs: object) -> tuple[str, None]:
        return "a" * 40, None

    async def _head(_path: Path) -> str:
        return "a" * 40

    async def _task_tag(_workspace_id: str) -> None:
        return None

    async def _address_thread(**_kwargs: object) -> str:
        raise AgentVerdictProtocolError(reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION)

    async def _run_command(*_args: object, **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _unexpected_forge_call(**_kwargs: object) -> None:
        forge_calls.append("called")

    async def _owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(fix_cycle, "_owned_paths_for_prompt_or_empty", _owned_paths)
    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _pre_existing_dirty_repair_worktree_result=_none,
        _abandon_unpublished_comment_repairs=_no_abandoned_repairs,
        _repair_operation_start_head_result=_start,
        _resolve_task_tag=_task_tag,
        _rev_parse_head=_head,
        _address_thread=_address_thread,
        _runner_config=SimpleNamespace(max_fix_cycle_passes=1),
        _deps=SimpleNamespace(
            runner=SimpleNamespace(run=_run_command),
            gh=SimpleNamespace(fetch_pr_status=_unexpected_forge_call),
        ),
    )

    result = await fix_cycle._run_fix_cycle(
        runner,
        workspace_id=workspace_id,
        repo=RepoRef(owner="owner", name="repo"),
        pr_number=848,
        pr_head_sha="a" * 40,
        initial_threads=(
            ReviewThread(
                thread_id="T_protocol",
                path="src/example.py",
                line=1,
                body_excerpt="please repair",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="fix/protocol",
        compose_project="awf_ws_protocol_terminal",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert result.failure_reason is FailureReason.agent_failure
    assert forge_calls == []


@pytest.mark.unit
async def test_fix_cycle_records_local_terminal_head_on_terminal_protocol_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_protocol_terminal_head"
    worktree = tmp_path / workspace_id
    worktree.mkdir()
    remote_head = "a" * 40
    repair_head = "b" * 40
    call_count = 0

    async def _none(**_kwargs: object) -> None:
        return None

    async def _no_abandoned_repairs(**kwargs: object) -> tuple[str, None]:
        return str(kwargs["local_head"]), None

    async def _start(**_kwargs: object) -> tuple[str, None]:
        return remote_head, None

    async def _head(_path: Path) -> str:
        return repair_head

    async def _task_tag(_workspace_id: str) -> None:
        return None

    async def _address_thread(**_kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "fix_committed"
        raise AgentVerdictProtocolError(reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION)

    async def _run_command(*_args: object, **_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(fix_cycle, "_owned_paths_for_prompt_or_empty", _owned_paths)
    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _pre_existing_dirty_repair_worktree_result=_none,
        _abandon_unpublished_comment_repairs=_no_abandoned_repairs,
        _repair_operation_start_head_result=_start,
        _resolve_task_tag=_task_tag,
        _rev_parse_head=_head,
        _address_thread=_address_thread,
        _runner_config=SimpleNamespace(max_fix_cycle_passes=1),
        _deps=SimpleNamespace(
            runner=SimpleNamespace(run=_run_command),
            gh=SimpleNamespace(fetch_pr_status=lambda **_kwargs: None),
        ),
    )

    result = await fix_cycle._run_fix_cycle(
        runner,
        workspace_id=workspace_id,
        repo=RepoRef(owner="owner", name="repo"),
        pr_number=848,
        pr_head_sha=remote_head,
        initial_threads=(
            ReviewThread(
                thread_id="T_protocol_1",
                path="src/example.py",
                line=1,
                body_excerpt="first",
                author="reviewer",
            ),
            ReviewThread(
                thread_id="T_protocol_2",
                path="src/example.py",
                line=2,
                body_excerpt="second",
                author="reviewer",
            ),
        ),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="fix/protocol",
        compose_project="awf_ws_protocol_terminal_head",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.details is not None
    assert result.details.get("local_terminal_head_sha") == repair_head
    evidence = result.failure_evidence()
    assert evidence.get("local_terminal_head_sha") == repair_head


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_records_retryable_push_failure_provenance(
    tmp_path: Path,
) -> None:
    """A retryable push failure leaves the same unpublished commit as a terminal one.

    The next cycle abandons it only with recorded provenance; without it the
    abandon path fails closed on ``COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING``
    instead of re-addressing and retrying the push (PRRT_kwDOSJAM6s6fp2uF).
    """
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="fatal: the remote end hung up unexpectedly",
        reason_code="GIT_PUSH_FAILED",
    )
    repair_head = "b" * 40

    async def _head(_path: Path) -> str:
        return repair_head

    runner = SimpleNamespace(_rev_parse_head=_head)
    with structlog.testing.capture_logs() as captured:
        result = await fix_cycle._enrich_failed_fix_cycle_result(
            runner,
            push_result,
            worktree_path=tmp_path,
            operation_start_head="a" * 40,
        )

    assert result.terminal_monitor_failure is False
    assert result.details is not None
    assert result.details.get("local_terminal_head_sha") == repair_head
    assert result.failure_evidence().get("local_terminal_head_sha") == repair_head
    # The retryable exit stays in ``monitoring_pr``, so the recorded head never
    # reaches a terminal-failure workspace event — the log is its only live trace.
    recorded = [
        entry
        for entry in captured
        if entry["event"] == "monitor.fix_cycle_unpublished_repair_head_recorded"
    ]
    assert recorded == [
        {
            "event": "monitor.fix_cycle_unpublished_repair_head_recorded",
            "log_level": "info",
            "reason_code": "GIT_PUSH_FAILED",
            "terminal": False,
            "local_head_sha": repair_head,
        }
    ]


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_skips_resynced_push_failure(
    tmp_path: Path,
) -> None:
    """A rejection recovered by resync left HEAD at the start commit — nothing to own."""
    start_head = "a" * 40
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="push rejected",
        reason_code="GIT_PUSH_FAILED",
        recovered_by_resync=True,
    )

    async def _head(_path: Path) -> str:
        return start_head

    runner = SimpleNamespace(_rev_parse_head=_head)
    result = await fix_cycle._enrich_failed_fix_cycle_result(
        runner,
        push_result,
        worktree_path=tmp_path,
        operation_start_head=start_head,
    )

    assert result is push_result
    assert result.details is None or "local_terminal_head_sha" not in result.details


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_skips_protected_scope_pause(
    tmp_path: Path,
) -> None:
    """A pause into ``blocked`` preserves its commit for an operator, not for abandonment."""
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="protected scope paused",
        reason_code="PROTECTED_SCOPE_PAUSED",
        details={"preserved_head_sha": "b" * 40},
        paused_into_blocked=True,
    )

    async def _head(_path: Path) -> str:  # pragma: no cover - must not be called
        raise AssertionError("paused results must not be fingerprinted")

    runner = SimpleNamespace(_rev_parse_head=_head)
    result = await fix_cycle._enrich_failed_fix_cycle_result(
        runner,
        push_result,
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert result is push_result
    assert result.details is not None
    assert "local_terminal_head_sha" not in result.details


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_marks_provenance_unavailable_when_rev_parse_fails(
    tmp_path: Path,
) -> None:
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="protocol violation",
        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
        failure_reason=FailureReason.agent_failure,
    )

    async def _head_raises(_path: Path) -> str:
        raise OSError("rev-parse failed")

    runner = SimpleNamespace(_rev_parse_head=_head_raises)
    result = await fix_cycle._enrich_failed_fix_cycle_result(
        runner,
        push_result,
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert result is not push_result
    assert result.terminal_monitor_failure is True
    assert result.details is not None
    assert result.details.get("local_terminal_head_provenance_unavailable") is True
    assert "local_terminal_head_sha" not in result.details


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_marks_provenance_unavailable_when_rev_parse_empty(
    tmp_path: Path,
) -> None:
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="protocol violation",
        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
        failure_reason=FailureReason.agent_failure,
    )

    async def _head(_path: Path) -> str | None:
        return None

    runner = SimpleNamespace(_rev_parse_head=_head)
    result = await fix_cycle._enrich_failed_fix_cycle_result(
        runner,
        push_result,
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert result.details is not None
    assert result.details.get("local_terminal_head_provenance_unavailable") is True


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_attaches_head_for_terminal_failure(
    tmp_path: Path,
) -> None:
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="protocol violation",
        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
        failure_reason=FailureReason.agent_failure,
    )
    repair_head = "b" * 40

    async def _head(_path: Path) -> str:
        return repair_head

    runner = SimpleNamespace(_rev_parse_head=_head)
    result = await fix_cycle._enrich_failed_fix_cycle_result(
        runner,
        push_result,
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert result is not push_result
    assert result.terminal_monitor_failure is True
    assert result.details is not None
    assert result.details.get("local_terminal_head_sha") == repair_head


@pytest.mark.unit
async def test_enrich_failed_fix_cycle_result_propagates_unexpected_rev_parse_error(
    tmp_path: Path,
) -> None:
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="protocol violation",
        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
        failure_reason=FailureReason.agent_failure,
    )

    async def _head_raises(_path: Path) -> str:
        raise RuntimeError("broken test double")

    runner = SimpleNamespace(_rev_parse_head=_head_raises)
    with pytest.raises(RuntimeError, match="broken test double"):
        await fix_cycle._enrich_failed_fix_cycle_result(
            runner,
            push_result,
            worktree_path=tmp_path,
            operation_start_head="a" * 40,
        )
