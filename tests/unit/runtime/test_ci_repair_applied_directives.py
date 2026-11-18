"""CI repair sees applied operator directives and parks conflicts (#1256)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.prompt_evidence import EVIDENCE_QUOTE_PREFIX
from awf.runtime.monitor_prompts import fix_ci_prompt
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    ReportCiFailure,
    decide,
)
from awf.runtime.pr_monitor_models import ActionsRunsState
from awf.runtime.pr_monitor_runner import ci_ops
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    AppliedOperatorDirective,
    append_applied_operator_directive,
    ci_repair_operator_decision_conflict_reason,
    load_applied_operator_directives,
    mark_ci_repair_operator_decision_conflict,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

_FAILURES = (
    CheckFailure(
        name="review-bot",
        conclusion="FAILURE",
        log_excerpt="Finding: restore the removed experimental block",
        run_id="r-bot",
        test_node_ids=("review-bot::finding",),
        assertion_snippets=("restore the removed experimental block",),
    ),
)
_OTHER_FAILURES = (
    CheckFailure(
        name="unit-tests",
        conclusion="FAILURE",
        log_excerpt="FAILED tests/unit/test_x.py::test_y - AssertionError",
        run_id="r-unit",
        test_node_ids=("tests/unit/test_x.py::test_y",),
        assertion_snippets=("assert 1 == 2",),
    ),
)
_HEAD = "b34a78bf86abcdef0123456789abcdef01234567"
_PUBLISHED_HOSTED_HEAD = "c5e01942e6abcdef0123456789abcdef01234567"
_FAKE_REPO = SimpleNamespace(slug=lambda: "dimileeh/awf")
_CONFLICT_STDOUT = (
    "I will not re-add the removed code.\n"
    "AWF-VERDICT: NEEDS_HUMAN: review-bot conflicts with operator decision "
    "op-guide-1: restoring the block would undo the operator decision\n"
)


class _MonitorAgentServiceRecoveryRunner(SimpleNamespace):
    async def _run_monitor_agent_with_service_recovery(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        log_source: str,
        command_evidence: list[str] | None = None,
        operation_start_head: str | None = None,
        state: object | None = None,
        timeout_rerun_floor_sink: list[str] | None = None,
        timeout_rerun_dirty_sink: object | None = None,
        timeout_preservation_sink: list[str] | None = None,
    ) -> AgentRunResult:
        del operation_start_head, state, timeout_rerun_floor_sink
        del timeout_rerun_dirty_sink, timeout_preservation_sink
        result = await self._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            workspace_id=workspace_id,
            log_source=log_source,
        )
        append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
        return result


def _status(
    *,
    head_sha: str = _HEAD,
    failures: tuple[CheckFailure, ...] = _FAILURES,
) -> PRStatus:
    return PRStatus(
        number=1260,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.FAILURE,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=failures,
        actions_runs=ActionsRunsState.SETTLED,
    )


def _seed_directive(state: MonitorState, *, operation_id: str = "op-guide-1") -> None:
    append_applied_operator_directive(
        state,
        AppliedOperatorDirective(
            directive="Remove the experimental block entirely.",
            operation_id=operation_id,
            applied_at="2026-11-17T17:56:00+00:00",
            head_sha=_HEAD,
        ),
    )


async def _open_pr_post_action_recheck(**_kwargs: object) -> _GitPushResult | None:
    return None


def _ci_fix_runner(
    *,
    tmp_path: Path,
    stdout: str = "",
    agent_returncode: int = 0,
    commit_calls: list[dict[str, Any]] | None = None,
    push_calls: list[dict[str, Any]] | None = None,
    rollback_calls: list[dict[str, Any]] | None = None,
    rollback_ok: bool = True,
    rollback_cause: str = "reset_failed",
    rollback_message: str = ("CI repair residue rollback failed: git reset --hard failed."),
) -> _MonitorAgentServiceRecoveryRunner:
    commit_calls = commit_calls if commit_calls is not None else []
    push_calls = push_calls if push_calls is not None else []
    rollback_calls = rollback_calls if rollback_calls is not None else []

    async def _resolve_task_tag(_workspace_id: str) -> None:
        return None

    async def _pre_existing(**_kwargs: object) -> None:
        return None

    async def _suppress(_workspace_id: str) -> bool:
        return False

    async def _head(**_kwargs: object) -> tuple[str, None]:
        return _HEAD, None

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return _HEAD

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        commit_calls.append(dict(kwargs))
        return False

    async def _psb(**_kwargs: object) -> None:
        return None

    async def _validated(**kwargs: object) -> _GitPushResult:
        push_calls.append(dict(kwargs))
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _handle_provider_agent_run_error(
        _workspace_id: str,
        _exc: AgentRunError,
        *,
        state: object | None = None,
    ) -> str:
        del state
        # Deterministic/terminal: return without raising so the CI repair path
        # continues into the validated-push finalizer (PRRT_kwDOSJAM6s64xqaf).
        return "terminal"

    async def _adapter_run(**_kwargs: object) -> AgentRunResult:
        if agent_returncode != 0:
            raise AgentRunError(
                agent="cursor",
                result=CommandResult(
                    returncode=agent_returncode,
                    stdout=stdout,
                    stderr="cli exited nonzero",
                ),
            )
        return AgentRunResult(returncode=0, stdout=stdout, stderr="")

    async def _rollback(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        restore_ref: str | None,
        **_kwargs: object,
    ) -> ci_ops._CiFixResidueRollbackResult:
        rollback_calls.append(
            {
                "workspace_id": workspace_id,
                "worktree_path": worktree_path,
                "restore_ref": restore_ref,
            }
        )
        if rollback_ok:
            return ci_ops._CiFixResidueRollbackResult(ok=True)
        return ci_ops._CiFixResidueRollbackResult(
            ok=False,
            cause=rollback_cause,
            message=rollback_message,
        )

    async def _hosted_pr_identity_for_workspace(
        _workspace_id: str,
        *,
        state: object | None = None,
    ) -> dict[str, object]:
        del state
        return {
            "head_repo_url": "https://github.com/dimileeh/awf.git",
            "head_ref": "awf/ws_ci",
        }

    return _MonitorAgentServiceRecoveryRunner(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _resolve_task_tag=_resolve_task_tag,
        _pre_existing_dirty_repair_worktree_result=_pre_existing,
        _provider_recovery_suppresses_cli=_suppress,
        _repair_operation_start_head_result=_head,
        _rev_parse_head=_rev_parse_head,
        _commit_dirty_worktree=_commit_dirty_worktree,
        _protected_scope_push_block=_psb,
        _post_action_pr_terminal_push_result_if_moot=_open_pr_post_action_recheck,
        _validated_git_push_result=_validated,
        _handle_provider_agent_run_error=_handle_provider_agent_run_error,
        _hosted_pr_identity_for_workspace=_hosted_pr_identity_for_workspace,
        _deps=SimpleNamespace(adapter=SimpleNamespace(run=_adapter_run, is_hosted=False)),
        _rollback=_rollback,
    )


@pytest.fixture(autouse=True)
def _stub_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _repair(**_kwargs: object) -> bool:
        return True

    async def _owned_paths(_self: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(ci_ops, "repair_agent_runtime_ownership", _repair)
    monkeypatch.setattr(ci_ops, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(ci_ops, "_owned_paths_for_prompt", _owned_paths)


@pytest.mark.unit
async def test_run_ci_fix_passes_applied_directives_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt_kwargs: list[dict[str, Any]] = []
    runner = _ci_fix_runner(tmp_path=tmp_path)
    original_prompt = ci_ops.fix_ci_prompt

    def _capture_prompt(**kwargs: object) -> str:
        prompt_kwargs.append(dict(kwargs))
        return original_prompt(**kwargs)

    monkeypatch.setattr(ci_ops, "fix_ci_prompt", _capture_prompt)

    state = MonitorState()
    _seed_directive(state)

    with structlog.testing.capture_logs() as captured:
        await ci_ops._run_ci_fix(
            runner,
            repo=_FAKE_REPO,
            pr_number=1260,
            failures=_FAILURES,
            compose_project="proj",
            compose_file=Path("compose.yml"),
            workspace_id="ws_ci",
            remote_branch="awf/ws_ci",
            status=_status(),
            state=state,
        )

    assert prompt_kwargs
    records = prompt_kwargs[0].get("applied_operator_directives")
    assert records is not None
    assert len(records) == 1
    assert records[0].operation_id == "op-guide-1"

    audit_events = [
        event
        for event in captured
        if event.get("event") == "monitor.ci_repair_applied_operator_directives"
    ]
    assert len(audit_events) == 1
    assert audit_events[0]["count"] == 1
    assert audit_events[0]["operation_ids"] == ["op-guide-1"]
    assert audit_events[0]["workspace_id"] == "ws_ci"
    assert audit_events[0]["pr_number"] == 1260
    assert audit_events[0]["head_sha"] == _HEAD
    assert "directive" not in audit_events[0]
    assert "Remove the experimental block" not in str(audit_events[0])


@pytest.mark.unit
@pytest.mark.parametrize("agent_returncode", [0, 1])
async def test_ci_repair_needs_human_conflict_parks_without_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_returncode: int,
) -> None:
    """Park conflict for success and nonzero CLI exits (PRRT_kwDOSJAM6s64xqaf).

    A valid AWF-VERDICT: NEEDS_HUMAN conflict with applied directives must roll
    back and park before commit/push even when the adapter raises AgentRunError.
    """
    commit_calls: list[dict[str, Any]] = []
    push_calls: list[dict[str, Any]] = []
    rollback_calls: list[dict[str, Any]] = []
    runner = _ci_fix_runner(
        tmp_path=tmp_path,
        stdout=_CONFLICT_STDOUT,
        agent_returncode=agent_returncode,
        commit_calls=commit_calls,
        push_calls=push_calls,
        rollback_calls=rollback_calls,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        runner._rollback,
    )

    state = MonitorState()
    _seed_directive(state)
    status = _status()

    result = await ci_ops._run_ci_fix(
        runner,
        repo=_FAKE_REPO,
        pr_number=1260,
        failures=_FAILURES,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
        status=status,
        state=state,
    )

    assert result.parked_needs_human is True
    assert result.pushed is False
    assert result.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert "conflicts with operator decision" in (result.stderr or "")
    assert commit_calls == []
    assert push_calls == []
    assert rollback_calls
    assert (
        ci_repair_operator_decision_conflict_reason(
            state, head_sha=status.head_sha, failures=_FAILURES
        )
        is not None
    )

    action = decide(status, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert "conflicts with operator decision" in (action.message or "")
    assert action.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"

    again = decide(status, state, MonitorConfig(auto_merge=True))
    assert isinstance(again, NotifyHuman)
    assert again.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"


@pytest.mark.unit
async def test_ci_repair_needs_human_conflict_rewinds_hosted_published_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted NEEDS_HUMAN conflict must rewind the published PR head (PRRT_kwDOSJAM6s64x4K2).

    Local-only residue rollback leaves the hosted repair on the remote while the
    park anchors the old status.head_sha; the next poll then re-dispatches CI
    repair. Mirror comment_verdict_rollback hosted remote reconciliation.
    """
    commit_calls: list[dict[str, Any]] = []
    push_calls: list[dict[str, Any]] = []
    rollback_calls: list[dict[str, Any]] = []
    remote_rollbacks: list[dict[str, object]] = []
    runner = _ci_fix_runner(
        tmp_path=tmp_path,
        stdout=_CONFLICT_STDOUT,
        commit_calls=commit_calls,
        push_calls=push_calls,
        rollback_calls=rollback_calls,
    )
    runner._deps.adapter.is_hosted = True

    async def _hosted_agent_with_published_head(
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        log_source: str,
        command_evidence: list[str] | None = None,
        operation_start_head: str | None = None,
        state: object | None = None,
        timeout_rerun_floor_sink: list[str] | None = None,
        timeout_rerun_dirty_sink: object | None = None,
        timeout_preservation_sink: list[str] | None = None,
    ) -> AgentRunResult:
        del (
            timeout_rerun_floor_sink,
            timeout_rerun_dirty_sink,
            timeout_preservation_sink,
            operation_start_head,
        )
        result = await runner._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            workspace_id=workspace_id,
            log_source=log_source,
        )
        append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
        if isinstance(state, MonitorState):
            state.last_push_sha = _PUBLISHED_HOSTED_HEAD
            state.hosted_terminal_head_advanced = True
        return result

    async def _record_remote_rollback(*_args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    runner._run_monitor_agent_with_service_recovery = _hosted_agent_with_published_head
    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        runner._rollback,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    state = MonitorState(last_push_sha=_HEAD)
    _seed_directive(state)
    status = _status()

    result = await ci_ops._run_ci_fix(
        runner,
        repo=_FAKE_REPO,
        pr_number=1260,
        failures=_FAILURES,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
        status=status,
        state=state,
    )

    assert result.parked_needs_human is True
    assert result.pushed is False
    assert result.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert commit_calls == []
    assert push_calls == []
    assert rollback_calls
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == _HEAD
    assert remote_rollbacks[0]["expected_remote_head_sha"] == _PUBLISHED_HOSTED_HEAD
    assert state.last_push_sha == _HEAD
    assert state.hosted_terminal_head_advanced is False
    assert (
        ci_repair_operator_decision_conflict_reason(
            state, head_sha=status.head_sha, failures=_FAILURES
        )
        is not None
    )


@pytest.mark.unit
async def test_ci_repair_needs_human_conflict_hosted_remote_rollback_failure_parks_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted remote rewind failure parks pending; never fails the workspace."""
    from awf.runtime.pr_monitor_runner.ci_repair_conflict_rollback import (
        ci_repair_conflict_rollback_pending,
    )

    commit_calls: list[dict[str, Any]] = []
    push_calls: list[dict[str, Any]] = []
    rollback_calls: list[dict[str, Any]] = []
    runner = _ci_fix_runner(
        tmp_path=tmp_path,
        stdout=_CONFLICT_STDOUT,
        commit_calls=commit_calls,
        push_calls=push_calls,
        rollback_calls=rollback_calls,
    )
    runner._deps.adapter.is_hosted = True

    async def _hosted_agent_with_published_head(
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        log_source: str,
        command_evidence: list[str] | None = None,
        operation_start_head: str | None = None,
        state: object | None = None,
        timeout_rerun_floor_sink: list[str] | None = None,
        timeout_rerun_dirty_sink: object | None = None,
        timeout_preservation_sink: list[str] | None = None,
    ) -> AgentRunResult:
        del (
            timeout_rerun_floor_sink,
            timeout_rerun_dirty_sink,
            timeout_preservation_sink,
            operation_start_head,
        )
        result = await runner._deps.adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            workspace_id=workspace_id,
            log_source=log_source,
        )
        append_command_evidence(command_evidence, stdout=result.stdout, stderr=result.stderr)
        if isinstance(state, MonitorState):
            state.last_push_sha = _PUBLISHED_HOSTED_HEAD
            state.hosted_terminal_head_advanced = True
        return result

    async def _failed_remote_rollback(*_args: object, **_kwargs: object) -> bool:
        return False

    runner._run_monitor_agent_with_service_recovery = _hosted_agent_with_published_head
    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        runner._rollback,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _failed_remote_rollback,
    )

    state = MonitorState(last_push_sha=_HEAD)
    _seed_directive(state)
    status = _status()

    result = await ci_ops._run_ci_fix(
        runner,
        repo=_FAKE_REPO,
        pr_number=1260,
        failures=_FAILURES,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
        status=status,
        state=state,
    )

    assert result.parked_needs_human is True
    assert result.pushed is False
    assert result.reason_code == "CI_REPAIR_CONFLICT_ROLLBACK_PENDING"
    assert f"remote head {_PUBLISHED_HOSTED_HEAD} not rewound to {_HEAD}" in (result.stderr or "")
    assert commit_calls == []
    assert push_calls == []
    assert rollback_calls
    assert (
        ci_repair_operator_decision_conflict_reason(
            state, head_sha=status.head_sha, failures=_FAILURES
        )
        is None
    )
    # Pending park is keyed by the unre wound published live head, not the
    # pre-repair status snapshot from the repair cycle (PRRT_kwDOSJAM6s64yp29).
    # Lookup by restore_ref / pre-repair head must still resolve it so retries
    # and decide() cannot miss while the forge status lags (PRRT_kwDOSJAM6s64yqJ9).
    pending = ci_repair_conflict_rollback_pending(
        state, head_sha=_PUBLISHED_HOSTED_HEAD, failures=_FAILURES
    )
    assert pending is not None
    assert pending.published_remote_head == _PUBLISHED_HOSTED_HEAD
    assert (
        ci_repair_conflict_rollback_pending(state, head_sha=status.head_sha, failures=_FAILURES)
        == pending
    )
    assert result.details is not None
    assert result.details.get("rollback_error", {}).get("cause") == "hosted_remote_rollback_failed"
    assert state.hosted_terminal_head_advanced is True
    assert state.last_push_sha == _PUBLISHED_HOSTED_HEAD
    live_status = _status(head_sha=_PUBLISHED_HOSTED_HEAD)
    action = decide(live_status, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == "CI_REPAIR_CONFLICT_ROLLBACK_PENDING"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rollback_cause", "rollback_message"),
    [
        ("reset_failed", "CI repair residue rollback failed: git reset --hard failed."),
        (
            "cleanup_failed",
            "Untracked CI repair residue cleanup failed (CLEANUP_FAILED): clean failed.",
        ),
    ],
)
async def test_ci_repair_needs_human_conflict_parks_pending_on_worktree_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_cause: str,
    rollback_message: str,
) -> None:
    """Worktree rollback failure parks pending; does not fail the workspace."""
    from awf.runtime.pr_monitor_runner.ci_repair_conflict_rollback import (
        ci_repair_conflict_rollback_pending,
    )

    commit_calls: list[dict[str, Any]] = []
    push_calls: list[dict[str, Any]] = []
    rollback_calls: list[dict[str, Any]] = []
    runner = _ci_fix_runner(
        tmp_path=tmp_path,
        stdout=_CONFLICT_STDOUT,
        commit_calls=commit_calls,
        push_calls=push_calls,
        rollback_calls=rollback_calls,
        rollback_ok=False,
        rollback_cause=rollback_cause,
        rollback_message=rollback_message,
    )
    monkeypatch.setattr(
        ci_ops,
        "_rollback_ci_fix_residue_before_provider_recovery",
        runner._rollback,
    )

    state = MonitorState()
    _seed_directive(state)
    status = _status()

    result = await ci_ops._run_ci_fix(
        runner,
        repo=_FAKE_REPO,
        pr_number=1260,
        failures=_FAILURES,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
        status=status,
        state=state,
    )

    assert result.parked_needs_human is True
    assert result.pushed is False
    assert result.reason_code == "CI_REPAIR_CONFLICT_ROLLBACK_PENDING"
    assert "leftover worktree edits" in (result.stderr or "")
    assert commit_calls == []
    assert push_calls == []
    assert rollback_calls
    assert (
        ci_repair_operator_decision_conflict_reason(
            state, head_sha=status.head_sha, failures=_FAILURES
        )
        is None
    )
    pending = ci_repair_conflict_rollback_pending(
        state, head_sha=status.head_sha, failures=_FAILURES
    )
    assert pending is not None
    assert pending.cause == rollback_cause
    assert result.details is not None
    assert result.details.get("phase") == "ci_repair_operator_decision_conflict"
    assert result.details.get("rollback_error", {}).get("cause") == rollback_cause
    assert result.details.get("rollback_error", {}).get("message") == rollback_message
    assert "conflicts with operator decision" in str(result.details.get("conflict_reason"))
    action = decide(status, state, MonitorConfig(auto_merge=True))
    assert isinstance(action, NotifyHuman)
    assert action.reason_code == "CI_REPAIR_CONFLICT_ROLLBACK_PENDING"


@pytest.mark.unit
def test_conflict_park_distinguishes_bitbucket_steps_sharing_pipeline_run_id() -> None:
    """Same Bitbucket pipeline UUID, different step → do not keep the old park.

    BitbucketClient stamps every failing step with the pipeline UUID as
    ``run_id``. Retry budgets correctly ignore step names when a run_id exists,
    but a conflict park must still distinguish which step failed so a later
    unrelated step failure can enter ReportCiFailure instead of replaying the
    previous operator-conflict NotifyHuman.
    """
    pipeline_uuid = "{shared-pipeline-uuid}"
    step_a = (
        CheckFailure(
            name="lint",
            conclusion="FAILURE",
            log_excerpt="ruff check failed",
            run_id=pipeline_uuid,
        ),
    )
    step_b = (
        CheckFailure(
            name="unit",
            conclusion="FAILURE",
            log_excerpt="pytest failed",
            run_id=pipeline_uuid,
        ),
    )
    state = MonitorState()
    _seed_directive(state)
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha=_HEAD,
        failures=step_a,
        reason="lint conflicts with operator decision op-guide-1: blocked",
    )
    assert isinstance(decide(_status(failures=step_a), state, MonitorConfig()), NotifyHuman)
    assert isinstance(
        decide(_status(failures=step_b), state, MonitorConfig()),
        ReportCiFailure,
    )


@pytest.mark.unit
def test_conflict_clears_for_new_guide_new_head_or_changed_failures() -> None:
    state = MonitorState()
    _seed_directive(state)
    status = _status()
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha=status.head_sha,
        failures=_FAILURES,
        reason="review-bot conflicts with operator decision op-guide-1: blocked",
    )
    conflict_action = decide(status, state, MonitorConfig())
    assert isinstance(conflict_action, NotifyHuman)
    assert conflict_action.reason_code == "CI_REPAIR_OPERATOR_DECISION_CONFLICT"

    new_head_state = MonitorState(
        threads_addressed_ids=dict(state.threads_addressed_ids),
    )
    new_head_status = _status(head_sha="c5e01942e6abcdef0123456789abcdef01234567")
    assert isinstance(
        decide(new_head_status, new_head_state, MonitorConfig()),
        ReportCiFailure,
    )

    changed_state = MonitorState(
        threads_addressed_ids=dict(state.threads_addressed_ids),
    )
    changed_status = _status(failures=_OTHER_FAILURES)
    assert isinstance(
        decide(changed_status, changed_state, MonitorConfig()),
        ReportCiFailure,
    )

    guide_state = MonitorState(
        threads_addressed_ids=dict(state.threads_addressed_ids),
        pending_operator_hint=OperatorHint(
            reason="follow-up guide",
            directive="Also leave workflows alone.",
            operation_id="op-guide-2",
            reason_code="OPERATOR_GUIDE",
        ),
    )
    _finalize_processed_operator_hint(
        guide_state,
        hint=guide_state.pending_operator_hint,
        acted_feedback_text="Also leave workflows alone.",
        head_sha=status.head_sha,
    )
    assert (
        ci_repair_operator_decision_conflict_reason(
            guide_state, head_sha=status.head_sha, failures=_FAILURES
        )
        is None
    )
    assert isinstance(decide(status, guide_state, MonitorConfig()), ReportCiFailure)


@pytest.mark.unit
def test_incident_regression_ci_prompt_contains_applied_guide_directive() -> None:
    """Guide applied, then failing check on next head → CI prompt quotes the directive."""
    hint = OperatorHint(
        reason="operator guide",
        directive="Remove the experimental block entirely.",
        operation_id="op-guide-1",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    _finalize_processed_operator_hint(
        state,
        hint=hint,
        acted_feedback_text=hint.directive,
        head_sha=_HEAD,
    )
    records = load_applied_operator_directives(state)
    assert records
    next_head = "c5e01942e6abcdef0123456789abcdef01234567"
    prompt = fix_ci_prompt(
        pr_number=1260,
        repo_slug="dimileeh/awf",
        failures=_FAILURES,
        applied_operator_directives=records,
    )
    assert "Applied operator decisions already on this PR" in prompt
    assert "bind only what they decided about the code now on this branch" in prompt
    assert "source_kind: operator_decision" in prompt
    assert [
        line for line in prompt.splitlines() if "Remove the experimental block entirely." in line
    ] == [f"{EVIDENCE_QUOTE_PREFIX}Remove the experimental block entirely."]
    assert isinstance(
        decide(_status(head_sha=next_head), state, MonitorConfig()),
        ReportCiFailure,
    )


@pytest.mark.unit
async def test_ci_repair_without_directives_ignores_needs_human_verdict(
    tmp_path: Path,
) -> None:
    push_calls: list[dict[str, Any]] = []
    runner = _ci_fix_runner(
        tmp_path=tmp_path,
        stdout=_CONFLICT_STDOUT,
        push_calls=push_calls,
    )

    result = await ci_ops._run_ci_fix(
        runner,
        repo=_FAKE_REPO,
        pr_number=1260,
        failures=_FAILURES,
        compose_project="proj",
        compose_file=Path("compose.yml"),
        workspace_id="ws_ci",
        remote_branch="awf/ws_ci",
        status=_status(),
        state=MonitorState(),
    )

    assert result.parked_needs_human is False
    assert result.reason_code != "CI_REPAIR_OPERATOR_DECISION_CONFLICT"
    assert result.pushed is True
    assert push_calls


@pytest.mark.unit
async def test_detect_hosted_ci_repair_conflict_published_head_edges(
    tmp_path: Path,
) -> None:
    """Detection skips non-hosted/blank restore and uses live HEAD when state lags."""
    runner = _ci_fix_runner(tmp_path=tmp_path)
    state = MonitorState(last_push_sha=_HEAD)

    assert (
        await ci_ops._detect_hosted_ci_repair_conflict_published_head(
            runner,
            worktree_path=tmp_path,
            state=state,
            restore_ref=_HEAD,
            operation_start_last_push_sha=_HEAD,
        )
        is None
    )

    runner._deps.adapter.is_hosted = True
    assert (
        await ci_ops._detect_hosted_ci_repair_conflict_published_head(
            runner,
            worktree_path=tmp_path,
            state=None,
            restore_ref=_HEAD,
            operation_start_last_push_sha=_HEAD,
        )
        is None
    )
    assert (
        await ci_ops._detect_hosted_ci_repair_conflict_published_head(
            runner,
            worktree_path=tmp_path,
            state=state,
            restore_ref="   ",
            operation_start_last_push_sha=_HEAD,
        )
        is None
    )

    async def _published_head(_worktree_path: Path) -> str | None:
        return _PUBLISHED_HOSTED_HEAD

    runner._rev_parse_head = _published_head
    assert (
        await ci_ops._detect_hosted_ci_repair_conflict_published_head(
            runner,
            worktree_path=tmp_path,
            state=state,
            restore_ref=_HEAD,
            operation_start_last_push_sha=_HEAD,
        )
        == _PUBLISHED_HOSTED_HEAD
    )

    async def _rev_parse_raises(_worktree_path: Path) -> str | None:
        raise OSError("gitdir missing")

    runner._rev_parse_head = _rev_parse_raises
    state.last_push_sha = _PUBLISHED_HOSTED_HEAD
    state.hosted_terminal_head_advanced = True
    assert (
        await ci_ops._detect_hosted_ci_repair_conflict_published_head(
            runner,
            worktree_path=tmp_path,
            state=state,
            restore_ref=_HEAD,
            operation_start_last_push_sha=_HEAD,
        )
        == _PUBLISHED_HOSTED_HEAD
    )


@pytest.mark.unit
async def test_rewind_hosted_ci_repair_conflict_remote_requires_identity_helper(
    tmp_path: Path,
) -> None:
    """Fail closed when the runner cannot resolve hosted PR identity for rewind."""
    runner = _ci_fix_runner(tmp_path=tmp_path)
    runner._deps.adapter.is_hosted = True
    delattr(runner, "_hosted_pr_identity_for_workspace")
    state = MonitorState(last_push_sha=_PUBLISHED_HOSTED_HEAD)
    state.hosted_terminal_head_advanced = True

    result = await ci_ops._rewind_hosted_ci_repair_conflict_remote(
        runner,
        workspace_id="ws_ci",
        state=state,
        restore_ref=_HEAD,
        published_remote_head=_PUBLISHED_HOSTED_HEAD,
        operation_start_last_push_sha=_HEAD,
    )

    assert result.ok is False
    assert result.cause == "hosted_remote_rollback_failed"
    assert state.hosted_terminal_head_advanced is True
    assert state.last_push_sha == _PUBLISHED_HOSTED_HEAD
