"""Executor recovery tests for carried plan-conformance gaps."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.executor import runtime_setup_replay
from awf.db.enums import FailureReason, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.runtime.operator_hints import operator_hint_from_threads, persist_operator_hint
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    AppliedOperatorDirective,
    append_applied_operator_directive,
    load_applied_operator_directives,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)
from tests.unit.control.test_executor_monitor_recovery_parts.test_executor_monitor_recovery_part_002 import (
    _all_adapter_args,
    _all_push_and_pr_create_calls,
    _make_executor,
    _queue_existing_pr_push,
    _queue_open_pr_lifecycle_snapshot,
    _queue_push_and_pr,
    _queue_rebase_recovery,
    _queue_validation_head,
    _seed_ready_workspace_no_recovery,
    _seed_ready_workspace_with_recovery,
    factory,
    fake,
)
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import _service

_IMPORTED_FIXTURES = (factory, fake)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "forge_calls", "failure"),
    [
        ("unpublished", 3, None),
        ("already_posted", 2, None),
        ("settled", 0, None),
        ("post_failed", 3, "Comment transport failed"),
        ("missing_gaps", 0, "stored gap details are missing"),
        ("superseded", 2, None),
        ("operator-park-posted", 2, None),
        ("operator-park-unpublished", 3, None),
        ("operator-park-tip-mismatch", 1, "does not contain the validated tip"),
    ],
)
async def test_remonitor_requires_pending_carried_gaps_on_retained_pr(
    factory: async_sessionmaker[AsyncSession],
    fake: FakeCommandRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    forge_calls: int,
    failure: str | None,
) -> None:
    parked = scenario.startswith("operator-park-")
    reason = f"{PLAN_CONFORMANCE_UNSATISFIED}: Add a regression test"
    heading = "### Unresolved plan-conformance gaps\n\n"
    notice = heading + "- **test_work**: Add a regression test"
    if parked:
        notice = (
            heading + f"`{PLAN_CONFORMANCE_UNSATISFIED}`\n\n"
            "AWF validation passed. Use `workspace guide --directive` to address these gaps.\n\n"
            "- **test_work**: Add a regression test"
        )
    pr_url = "https://github.com/x/y/pull/42"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n")
    ws_id = await _seed_monitoring_pr(factory, pr_url=pr_url, compose_file_path=str(compose_file))
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        ws.monitor_last_commit_sha = "d" * 40
        ws.block_planning_conformance_handoff = {
            "carried_gaps": (
                []
                if scenario == "missing_gaps"
                else [{"kind": "test_work", "detail": "Add a regression test"}]
            )
        }
        if parked:
            ws.block_planning_conformance_handoff["conformance_park_head_sha"] = "d" * 40
            ws.monitor_threads_addressed = persist_operator_hint(
                {},
                OperatorHint(
                    reason=reason, reason_code=PLAN_CONFORMANCE_UNSATISFIED, status="needs_human"
                ),
            )
        await repo.add_audit_event(
            ws,
            event_type="workspace.audit.pr_created",
            actor="executor",
            action="pr_create",
            outcome="reused",
            reason_code="PR_UPDATED",
            pr_url=pr_url,
            pr_number=42,
            evidence={"conformance_comment_pending": scenario != "settled"},
        )
        await repo.transition(
            ws, to=WorkspaceStatus.failed, reason_code="PR_CONFORMANCE_COMMENT_FAILED"
        )
        ws.failure_reason = FailureReason.infrastructure_failure.value
        ws.failure_message = "Initial publishing attempt failed"
        await session.commit()

        service, _, _ = _service(session)
        await service.remonitor_workspace(ws_id, reason="Retry the notice" if parked else None)
        ws.monitor_claimed_by = "original-worker"
        await session.commit()

    if parked:
        fake.respond_when(
            lambda args: (
                args[:3] == ["gh", "api", "graphql"] and any("headRefOid" in arg for arg in args)
            ),
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "merged": False,
                                "closed": False,
                                "headRefName": "awf/example",
                                "headRefOid": "" if scenario.endswith("mismatch") else "d" * 40,
                            }
                        }
                    }
                }
            ),
        )
    nodes = (
        [{"body": notice}]
        if scenario in {"already_posted", "superseded", "operator-park-posted"}
        else []
    )
    comments = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
    fake.respond_when(
        lambda args: args[:3] == ["gh", "api", "graphql"],
        stdout=json.dumps({"data": {"repository": {"pullRequest": {"comments": comments}}}}),
    )
    fake.respond_when(
        lambda args: args[:3] == ["gh", "pr", "comment"],
        returncode=1 if scenario == "post_failed" else 0,
        stderr="Comment transport failed" if scenario == "post_failed" else "",
    )
    if scenario == "superseded":
        run_command = fake.run

        async def take_over_after_comments(args: list[str], **kwargs: Any) -> CommandResult:
            result = await run_command(args, **kwargs)
            if args[:3] == ["gh", "api", "graphql"]:
                async with factory() as session:
                    ws = await WorkspaceRepository(session).get(ws_id)
                    assert ws is not None
                    ws.monitor_claimed_by = "replacement-worker"
                    await session.commit()
            return result

        monkeypatch.setattr(fake, "run", take_over_after_comments)

    def actual_forge_calls() -> list[list[str]]:
        return [call.args for call in fake.calls if call.args[0] == "gh"]

    async def assert_notice_checked(**_kwargs: Any) -> None:
        assert len(actual_forge_calls()) == forge_calls

    monitor_run = AsyncMock(side_effect=assert_notice_checked)
    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        pr_monitor_factory=lambda *_args, **_kwargs: SimpleNamespace(run=monitor_run),
    )
    monkeypatch.setattr(
        runtime_setup_replay, "capture_agent_container_identity", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        executor, "_restart_compose_runtime_for_monitor_resume", AsyncMock(return_value=True)
    )
    await executor.resume_pr_monitor(ws_id)

    if failure or scenario == "superseded":
        monitor_run.assert_not_awaited()
    else:
        monitor_run.assert_awaited_once()
    if scenario == "unpublished":
        await executor.resume_pr_monitor(ws_id)
        assert monitor_run.await_count == 2
    calls = actual_forge_calls()
    assert len(calls) == forge_calls
    if scenario in {"unpublished", "post_failed", "operator-park-unpublished"}:
        assert calls[-1][:4] == ["gh", "pr", "comment", "42"]
        assert calls[-1][-1] == notice
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.pr_url == pr_url
        publications = [
            event
            for event in ws.events
            if event.event_type == "workspace.audit.pr_conformance_comment"
        ]
        if failure:
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.infrastructure_failure.value
            assert ws.failure_message and failure in ws.failure_message
            last_transition = next(
                event
                for event in reversed(ws.events)
                if event.event_type == "workspace.state_changed"
            )
            assert last_transition.reason_code == "PR_CONFORMANCE_COMMENT_FAILED"
        else:
            assert ws.status == WorkspaceStatus.monitoring_pr.value
        if parked:
            hint = operator_hint_from_threads(ws.monitor_threads_addressed)
            assert hint is not None
            assert hint.reason == reason
            assert hint.status == "needs_human"
            if failure is None:
                assert ws.awaiting_human_reason == reason
        if scenario in {"unpublished", "already_posted"} or (parked and failure is None):
            (publication,) = publications
            assert publication.reason_code == "PR_CONFORMANCE_COMMENT_PUBLISHED"
            assert publication.payload and publication.payload["outcome"] == "succeeded"
        else:
            assert publications == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "recovery_mode", "operation_type", "retained_pr_state"),
    [
        ("pr_monitor", "validate_only", OperationType.validate, "open"),
        ("operator_api", "rebase_only", OperationType.rebase, "open"),
        ("pr_monitor", "validate_only", OperationType.validate, "closed"),
        ("pr_monitor", "validate_only", OperationType.validate, "merged"),
    ],
)
async def test_carried_gap_publication_during_existing_pr_recovery(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    source: str,
    recovery_mode: str,
    operation_type: OperationType,
    retained_pr_state: str,
) -> None:
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory, source=source, recovery_mode=recovery_mode, operation_type=operation_type
    )
    advisory = AppliedOperatorDirective(
        directive="test_work: Add a regression test",
        operation_id=ws_id,
        applied_at="2026-01-01T00:00:00+00:00",
        head_sha="d" * 40,
        source="plan_conformance",
    )
    state = MonitorState()
    append_applied_operator_directive(state, advisory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.monitor_threads_addressed = state.threads_addressed_ids
        ws.block_planning_conformance_handoff = {
            "carried_gaps": [{"kind": "test_work", "detail": "Add a regression test"}],
        }
        await s.commit()

    validation_head = "d" * 40
    if recovery_mode == "rebase_only":
        _queue_rebase_recovery(fake)
        validation_head = "c" * 40
    fixed_head = "e" * 40
    _queue_validation_head(fake, head=validation_head)
    fake.queue_result(returncode=1, stderr="pytest: failed")
    fake.queue_result(returncode=0)  # adapter.run (fix pass)
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # diff --cached
    fake.queue_result(returncode=0)  # git commit
    _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    replacement_url = "https://github.com/x/y/pull/2"
    if retained_pr_state == "open":
        _queue_existing_pr_push(fake, head=fixed_head)
    else:
        _queue_push_and_pr(
            fake, pr_url=replacement_url, head=fixed_head, retained_pr_state=retained_pr_state
        )
    fake.respond_when(lambda args: args[:3] == ["gh", "pr", "comment"], returncode=0)

    await executor.execute(ws_id)

    assert monitor_calls == [ws_id]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.monitor_last_commit_sha == fixed_head
        directives = load_applied_operator_directives(
            MonitorState(threads_addressed_ids=ws.monitor_threads_addressed or {})
        )
        if retained_pr_state == "open":
            assert ws.pr_url == "https://github.com/x/y/pull/1"
            assert directives == (advisory,)
        else:
            assert ws.pr_url == replacement_url
            (directive,) = directives
            assert directive.source == advisory.source
            assert directive.directive == advisory.directive
    create_calls = [
        call for call in _all_push_and_pr_create_calls(fake) if call[:3] == ["gh", "pr", "create"]
    ]
    if retained_pr_state == "open":
        assert create_calls == []
    else:
        (create_call,) = create_calls
        body = create_call[create_call.index("--body") + 1]
        assert (
            "### Unresolved plan-conformance gaps\n\n- **test_work**: Add a regression test" in body
        )
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in fake.calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "expected_comment_count", "expected_error"),
    [
        ("unchanged", 1, None),
        ("changed", 1, None),
        ("comment_rejected", 2, "Comment transport failed"),
        ("closed", 0, "Could not verify that the pull request contains the validated tip"),
        ("lookup_failed", 0, "json parse"),
        ("unknown_pushed_tip", 0, "Could not verify the pushed tip before publishing gaps"),
        ("unknown_pr_number", 0, "Could not resolve the pull request and validated tip"),
        ("already_published", 0, None),
    ],
)
async def test_existing_pr_recovery_retries_pending_carried_gap_comment(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    scenario: str,
    expected_comment_count: int,
    expected_error: str | None,
) -> None:
    head_changed = scenario in {"changed", "unknown_pushed_tip"}
    source_head = "d" * 40
    fixed_head = "e" * 40
    section = "### Unresolved plan-conformance gaps\n\n- **test_work**: Add a regression test"

    def comment_calls() -> list[list[str]]:
        return [call.args for call in fake.calls if call.args[:3] == ["gh", "pr", "comment"]]

    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            assert len(comment_calls()) == expected_comment_count
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        max_fix_passes=1,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_ready_workspace_with_recovery(
        factory,
        source="operator_api",
        pr_url="https://github.com/x/y/unknown"
        if scenario == "unknown_pr_number"
        else "https://github.com/x/y/pull/1",
    )
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(ws_id)
        assert ws is not None
        if scenario == "unknown_pr_number":
            ws.pr_number = None
        ws.block_planning_conformance_handoff = {
            "carried_gaps": [{"kind": "test_work", "detail": "Add a regression test"}],
        }
        await repo.add_audit_event(
            ws,
            event_type="workspace.audit.pr_created",
            actor="executor",
            action="pr_create",
            outcome="reused",
            reason_code="PR_UPDATED",
            pr_url=ws.pr_url,
            pr_number=ws.pr_number,
            source_head_sha=source_head,
            evidence={
                "conformance_comment_pending": True,
                "conformance_comment_error": "Original publication failed",
            },
        )
        if scenario == "already_published":
            await repo.add_audit_event(
                ws,
                event_type="workspace.audit.pr_conformance_comment",
                actor="executor",
                action="pr_conformance_comment",
                outcome="succeeded",
                reason_code="PR_CONFORMANCE_COMMENT_PUBLISHED",
                pr_url=ws.pr_url,
            )
        await s.commit()

    _queue_validation_head(fake, head=source_head)
    if head_changed:
        fake.queue_result(returncode=1, stderr="pytest: failed")
        fake.queue_result(returncode=0)  # adapter.run (fix pass)
        fake.queue_result(returncode=0)  # git add -A
        fake.queue_result(returncode=0, stdout="src/fix.py\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        _queue_validation_head(fake, head=fixed_head)
    fake.queue_result(returncode=0, stdout="tests ok")
    if head_changed:
        _queue_existing_pr_push(
            fake, head=fixed_head, resolve_pushed_head=scenario != "unknown_pushed_tip"
        )
    elif scenario == "lookup_failed":
        fake.queue_result(stdout="not JSON")
    elif scenario in {"unchanged", "closed", "comment_rejected"}:
        _queue_open_pr_lifecycle_snapshot(fake, head_sha=source_head, closed=scenario == "closed")
        if scenario == "comment_rejected":
            _queue_open_pr_lifecycle_snapshot(fake, head_sha=source_head)
    fake.respond_when(
        lambda args: args[:3] == ["gh", "pr", "comment"],
        returncode=int(scenario == "comment_rejected"),
        stderr="Comment transport failed" if scenario == "comment_rejected" else "",
    )

    await executor.execute(ws_id)

    assert len(_all_push_and_pr_create_calls(fake)) == int(head_changed)
    assert [call[-1] for call in comment_calls()] == [section] * expected_comment_count
    assert monitor_calls == ([] if expected_error else [ws_id])
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == (
            WorkspaceStatus.failed.value if expected_error else WorkspaceStatus.monitoring_pr.value
        )
        assert ws.pr_number == (None if scenario == "unknown_pr_number" else 1)
        if expected_error:
            assert ws.events[-1].reason_code == "PR_CONFORMANCE_COMMENT_FAILED"
            assert expected_error in (ws.failure_message or "")
        elif not head_changed:
            assert any(
                event.event_type == "workspace.audit.pr_conformance_comment"
                and (event.payload or {}).get("outcome") == "succeeded"
                for event in ws.events
            )


@pytest.mark.unit
async def test_validate_only_recovery_zero_adapter_calls_on_clean_pass(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Even stricter than the existing skip-planning test: recovery must
    issue zero ``docker compose exec`` adapter invocations on a clean
    validation pass (no fix-cycle needed)."""
    executor = _make_executor(fake=fake, factory=factory, tmp_path=tmp_path)
    ws_id = await _seed_ready_workspace_with_recovery(factory)

    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")

    await executor.execute(ws_id)

    # Zero adapter calls of any kind — not planning, not execution, not
    # conformance, and not fix-cycle prompts.
    adapter_invocations = _all_adapter_args(fake)
    assert adapter_invocations == []


@pytest.mark.unit
@pytest.mark.parametrize("parked", [False, True])
async def test_validate_only_recovery_publishes_carried_conformance_gaps(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    parked: bool,
) -> None:
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(workspace_id)
                assert ws is not None
                assert ws.status == WorkspaceStatus.monitoring_pr.value
                assert ws.pr_url == "https://github.com/x/y/pull/1"
                if parked:
                    assert ws.awaiting_human_reason == reason
                (directive,) = load_applied_operator_directives(
                    MonitorState(threads_addressed_ids=ws.monitor_threads_addressed or {})
                )
                assert directive.source == "plan_conformance"
                assert directive.directive == "test_work: Add a regression test"
            monitor_calls.append(workspace_id)

    executor = _make_executor(
        fake=fake,
        factory=factory,
        tmp_path=tmp_path,
        pr_monitor_factory=lambda *_args, **_kwargs: _FakeMonitor(),
    )
    ws_id = await _seed_ready_workspace_no_recovery(factory)
    gap = {"kind": "test_work", "detail": "Add a regression test"}
    head = "d" * 40
    reason = f"{PLAN_CONFORMANCE_UNSATISFIED}: Add a regression test"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        metadata: dict[str, Any] = {"carried_gaps": [gap]}
        if parked:
            metadata.update(
                conformance_park_head_sha=head,
                validation_run_id="previous-run",
                conformance={
                    "summary": "Add a regression test",
                    "gaps": [gap["detail"]],
                    "iterations_used": 3,
                    "max_iterations": 3,
                    "plan_path": "docs/awf-plans/plan.md",
                    "report_path": "docs/awf-plans/conformance.json",
                },
            )
            ws.monitor_threads_addressed = persist_operator_hint(
                {},
                OperatorHint(
                    reason=reason, reason_code=PLAN_CONFORMANCE_UNSATISFIED, status="needs_human"
                ),
            )
        ws.block_planning_conformance_handoff = metadata
        await OperationRepository(s).create(
            workspace_id=ws_id,
            operation_type=OperationType.validate,
            payload={
                "source": "worker_restart",
                "recovery_mode": "validate_only",
                "source_head_sha": head,
            },
            idempotency_key=f"worker_restart:validate_only:{ws_id}",
        )
        await s.commit()

    _queue_validation_head(fake, head=head)
    fake.queue_result(returncode=0, stdout="tests ok")
    _queue_push_and_pr(fake, head=head if parked else "deadbeef01")
    if parked:
        _queue_open_pr_lifecycle_snapshot(fake, head_sha=head)
        fake.queue_result(
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                            }
                        }
                    }
                }
            )
        )
        fake.queue_result()  # PR gap comment

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    (create_call,) = (
        call for call in _all_push_and_pr_create_calls(fake) if call[:3] == ["gh", "pr", "create"]
    )
    body = create_call[create_call.index("--body") + 1]
    if parked:
        (notice,) = [
            call.args[-1] for call in fake.calls if call.args[:3] == ["gh", "pr", "comment"]
        ]
        assert PLAN_CONFORMANCE_UNSATISFIED in notice
        assert gap["detail"] in notice
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.block_planning_conformance_handoff["validation_run_id"] != "previous-run"
    else:
        assert (
            "### Unresolved plan-conformance gaps\n\n- **test_work**: Add a regression test" in body
        )
    assert monitor_calls == [ws_id]
