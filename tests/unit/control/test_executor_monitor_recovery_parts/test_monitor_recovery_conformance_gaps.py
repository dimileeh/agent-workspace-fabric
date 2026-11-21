"""Executor recovery tests for carried plan-conformance gaps."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    AppliedOperatorDirective,
    append_applied_operator_directive,
    load_applied_operator_directives,
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

_IMPORTED_FIXTURES = (factory, fake)


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
async def test_validate_only_recovery_publishes_carried_conformance_gaps(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    monitor_calls: list[str] = []

    class _FakeMonitor:
        async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(workspace_id)
                assert ws is not None
                assert ws.status == WorkspaceStatus.monitoring_pr.value
                assert ws.pr_url == "https://github.com/x/y/pull/1"
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
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        ws.block_planning_conformance_handoff = {"carried_gaps": [gap]}
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
    _queue_push_and_pr(fake)

    await executor.execute(ws_id)

    assert _all_adapter_args(fake) == []
    (create_call,) = (
        call for call in _all_push_and_pr_create_calls(fake) if call[:3] == ["gh", "pr", "create"]
    )
    body = create_call[create_call.index("--body") + 1]
    assert "### Unresolved plan-conformance gaps\n\n- **test_work**: Add a regression test" in body
    assert monitor_calls == [ws_id]
