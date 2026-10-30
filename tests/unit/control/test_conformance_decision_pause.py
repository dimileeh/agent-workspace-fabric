"""Decision pauses preserve evidence and use the existing owner-fenced lifecycle."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.executor.planning_artifacts import handle_agent_planning_result
from awf.control.executor.types import _PlanningNeedsHuman
from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import parse_conformance_report
from tests.unit.control.test_enter_blocked_for_protected_violation import (
    _BASELINE,
    _executor,
    _get,
    _seed_running,
)
from tests.unit.control.test_enter_blocked_for_protected_violation import factory as factory
from tests.unit.runtime.test_conformance_decision import decision_payload


@pytest.mark.unit
@pytest.mark.parametrize("owner", ["worker-a", "stale-worker"])
async def test_decision_result_blocks_with_evidence_only_for_current_owner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    owner: str,
) -> None:
    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)
    worktree = tmp_path / "worktrees" / ws_id
    worktree.mkdir(parents=True)
    plan = worktree / f"{ws_id}.md"
    plan.write_text("Preserve this implementation plan.")
    (worktree / "implementation.py").write_text("existing work\n")
    profile = WorkspaceProfile.model_validate(
        {
            "name": "decision",
            "planning": {
                "required": True,
                "plan_path": "{workspace_id}.md",
                "conformance_report_path": "{workspace_id}.json",
            },
        }
    )
    result = _PlanningNeedsHuman(
        report=parse_conformance_report(decision_payload()),
        plan_path=Path(plan.name),
        report_path=Path(f"{ws_id}.json"),
        phase="conformance",
    )
    _, stop = await handle_agent_planning_result(
        executor,
        workspace_id=ws_id,
        ws=await _get(factory, ws_id),
        worktree_path=worktree,
        profile=profile,
        planning_failure=result,
        execution_owner_id=owner,
    )
    assert stop
    ws = await _get(factory, ws_id)
    assert (worktree / "implementation.py").read_text() == "existing work\n"
    if owner != "worker-a":
        assert ws.status == "running"
        assert not (tmp_path / "artifacts" / ws_id / "conformance.json").exists()
        return
    assert ws.status == WorkspaceStatus.blocked.value
    assert ws.block_type == "plan_conformance_decision"
    assert ws.block_reason_code == "PLAN_CONFORMANCE_NEEDS_HUMAN"
    assert ws.execution_claimed_by == owner
    assert ws.block_epoch == 1
    assert ws.block_violations == []
    assert ws.failure_reason is None
    async with factory() as session:
        current = await WorkspaceRepository(session).get(ws_id)
        assert current is not None
        transitions = [e for e in current.events if e.reason_code == "PLAN_CONFORMANCE_NEEDS_HUMAN"]
        assert transitions
        assert "Approve updating" in str(transitions[-1].payload)
    assert "needs_human" in (tmp_path / "artifacts" / ws_id / "conformance.json").read_text()


@pytest.mark.unit
@pytest.mark.parametrize("directive", [None, "", "  ", True, "Approve the dependency update."])
async def test_decision_resume_rechecks_hint_and_reuses_original_baseline(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    directive: object,
) -> None:
    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)
    await executor._persist_block_baseline_coverage(ws_id, baseline_coverage=_BASELINE)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.block_type = "plan_conformance_decision"
        ws.block_resume_phase = "post_validation_conformance"
        ws.block_epoch = 1
        ws.pending_operator_hint = {
            "reason": "operator answer",
            "directive": directive,
            "status": "pending",
        }
        await session.commit()
    result = await executor._begin_execution(
        ws_id,
        resume_reason="blocked",
        execution_owner_id="worker-a",
        execution_lease_expires_at=None,
    )
    if directive != "Approve the dependency update.":
        assert result is None
        ws = await _get(factory, ws_id)
        assert ws.status == "blocked"
        assert ws.block_epoch == 2
        assert ws.block_resume_phase == "post_validation_conformance"
        assert ws.pending_operator_hint is None
        return
    assert result is not None
    resumed, skip_agent, disable_fixes, baseline = result
    assert not skip_agent and not disable_fixes
    assert baseline == _BASELINE
    assert "answer the paused plan-conformance decision" in resumed.task_prompt
    assert resumed.task_prompt.endswith(directive)


@pytest.mark.unit
@pytest.mark.parametrize("directive", [None, "Retain the dependency pin."])
async def test_decision_survives_repeated_resume_setup_failures(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    directive: str | None,
) -> None:
    from awf.service.controls import WorkspaceGuideEmptyDirectiveError
    from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
        _service,
    )

    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)
    await executor._persist_block_baseline_coverage(ws_id, baseline_coverage=_BASELINE)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.block_type = "plan_conformance_decision"
        ws.block_epoch = 1
        await session.commit()

    for failure in range(2):
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            ws.status = "running"
            ws.pending_operator_hint = {
                "reason": "operator answer",
                "directive": "Approve the dependency update.",
                "status": "pending",
            }
            await session.commit()
        begin = await executor._begin_execution(
            ws_id,
            resume_reason="blocked",
            execution_owner_id="worker-a",
            execution_lease_expires_at=None,
        )
        assert begin is not None and not begin[1]
        assert await executor.enter_blocked_for_resume_setup_failure(
            workspace_id=ws_id,
            from_status=WorkspaceStatus.running,
            resume_phase="setup",
            execution_owner_id="worker-a",
        )
        ws = await _get(factory, ws_id)
        assert ws.status == "blocked"
        assert ws.block_type == "plan_conformance_decision"
        assert ws.block_reason_code == "RESUME_SETUP_FAILED"
        assert ws.block_epoch == failure + 2
        assert ws.pending_operator_hint is None
        # Initial conformance pauses have not persisted a validation handoff.
        assert ws.block_planning_conformance_handoff is None

    async with factory() as session:
        service, _, _ = _service(session)
        with pytest.raises(WorkspaceGuideEmptyDirectiveError):
            await service.guide_workspace(
                ws_id,
                directive="",
                reason="approved",
                grants=["pyproject.toml"],
                approve_policy_downgrade=True,
            )
        await session.rollback()

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        # Even an active legacy/corrupt grant must not bypass the decision.
        session.add(
            OperatorGrantAuditRecord(
                id=f"grant_{ws_id}",
                workspace_id=ws_id,
                operator="operator",
                reason="approved",
                normalized_path="pyproject.toml",
                block_epoch=ws.block_epoch,
            )
        )
        if directive:
            ws.pending_operator_hint = {
                "reason": "fresh answer",
                "directive": directive,
                "status": "pending",
            }
        await session.flush()
        assert (ws_id in await repo.list_resumable_blocked_ids(limit=10)) == bool(directive)
        ws.status = "running"
        await session.commit()

    result = await executor._begin_execution(
        ws_id,
        resume_reason="blocked",
        execution_owner_id="worker-a",
        execution_lease_expires_at=None,
    )
    if directive is None:
        assert result is None
        assert (await _get(factory, ws_id)).status == "blocked"
    else:
        assert result is not None
        resumed, skip_agent, disable_fixes, baseline = result
        assert not skip_agent and disable_fixes
        assert baseline == _BASELINE
        assert "answer the paused plan-conformance decision" in resumed.task_prompt
        assert resumed.task_prompt.endswith(directive)


@pytest.mark.unit
@pytest.mark.parametrize("workspace_exists", [False, True])
async def test_decision_setup_failure_ignores_missing_or_superseded_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_exists: bool,
) -> None:
    ws_id = await _seed_running(factory) if workspace_exists else "missing-workspace"
    executor = _executor(factory, tmp_path)
    if workspace_exists:
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            ws.block_type = "plan_conformance_decision"
            ws.block_epoch = 1
            await session.commit()

    assert not await executor.enter_blocked_for_resume_setup_failure(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        resume_phase="setup",
        execution_owner_id="stale-worker",
    )
    if workspace_exists:
        ws = await _get(factory, ws_id)
        assert ws.status == "running"
        assert ws.block_type == "plan_conformance_decision"
        assert ws.block_epoch == 1
        assert ws.execution_claimed_by == "worker-a"


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["create", "replace", "unlink"])
async def test_decision_artifact_failure_keeps_durable_question(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from awf.control.executor import planning_artifacts

    ws_id = await _seed_running(factory)
    executor = _executor(factory, tmp_path)
    directory = tmp_path / "artifacts" / ws_id
    directory.mkdir(parents=True)
    report_file = directory / "conformance.json"
    report_file.write_text('{"status":"satisfied"}')

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("simulated unavailable artifact storage")

    if failure == "create":
        monkeypatch.setattr(planning_artifacts.tempfile, "NamedTemporaryFile", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)
    if failure == "unlink":
        monkeypatch.setattr(Path, "unlink", fail)
    decision = _PlanningNeedsHuman(
        report=parse_conformance_report(decision_payload()),
        plan_path=Path("missing-plan.md"),
        report_path=Path("report.json"),
        phase="conformance",
    )
    await planning_artifacts.pause_for_conformance_decision(
        executor,
        workspace_id=ws_id,
        worktree_path=tmp_path / "checkout",
        decision=decision,
        from_status=WorkspaceStatus.running,
        execution_owner_id="worker-a",
    )
    ws = await _get(factory, ws_id)
    assert ws.status == "blocked"
    assert "Approve updating" in str([event.payload for event in ws.events])
    if failure != "unlink":
        assert not report_file.exists()


@pytest.mark.unit
async def test_cancelled_workspace_cannot_publish_decision(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from awf.control.executor.planning_artifacts import pause_for_conformance_decision

    ws_id = await _seed_running(factory)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.status = "cancelled"
        await session.commit()
    await pause_for_conformance_decision(
        _executor(factory, tmp_path),
        workspace_id=ws_id,
        worktree_path=tmp_path,
        decision=_PlanningNeedsHuman(
            report=parse_conformance_report(decision_payload()),
            plan_path=Path("plan.md"),
            report_path=Path("report.json"),
            phase="conformance",
        ),
        from_status=WorkspaceStatus.running,
        execution_owner_id="worker-a",
    )
    assert (await _get(factory, ws_id)).status == "cancelled"
    assert not (tmp_path / "artifacts" / ws_id).exists()
