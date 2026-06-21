from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from awf.control.executor.types import (
    _PlanningValidationHandoff,
)
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PlanConformanceReport,
    PlanConformanceStatus,
)
from tests.unit.control.test_executor_coverage_edges_parts.test_executor_coverage_edges_part_002 import (
    _executor_with_runner,
    _GitRestoreFakeRunner,
    _PlanningAdapter,
)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_restores_tracked_report_from_base_commit(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    # Pre-existing tracked-style report (e.g. from an earlier attempt).
    # It is modified by the agent during the conformance check; the executor
    # re-writes it from the satisfied report, then git restore succeeds and
    # restores the committed content.
    report_file.write_text(
        '{"status":"satisfied","summary":"stale success","gaps":[]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="validated-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    runner.queue_result(
        returncode=0, stdout="D  docs/awf-plans/ws_post.conformance.json\n"
    )  # git restore report path
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

    executor._record_post_validation_conformance_event = record_event  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_post.md"),
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(
            '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'
        ),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # Tracked report restored from base_commit: the original baseline copy
    # remains on disk so the worktree stays clean, instead of re-deleting the
    # restored file. Restoring from HEAD would resurrect any stale AWF-authored
    # report committed by an earlier fix pass; restoring from base_commit keeps
    # the original project content.
    assert report_file.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- "
        ":(literal)docs/awf-plans/ws_post.conformance.json" in call
        for call in joined_calls
    )
    assert not any("rm -- docs/awf-plans/ws_post.conformance.json" in call for call in joined_calls)
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_restores_from_head_when_base_differs(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    committed_report = (
        '{"status":"needs_iteration","summary":"committed stale miss","gaps":["fix me"]}'
    )
    # Pre-seed the worktree with the committed HEAD copy; the fake runner only
    # simulates restore behavior for untracked paths, so for tracked paths the
    # file content remains as-is. The adapter rewrites it, then the code writes
    # the satisfied report; we patch the writer to keep the committed report.
    report_file.write_text(committed_report, encoding="utf-8")
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="fix-pass-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    # base_commit restore exits 0 but leaves a staged modification relative to HEAD.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # Cleanliness check after base_commit restore still sees the staged path.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # HEAD restore exits 0 and cleans the path.
    runner.queue_result(returncode=0, stdout="")
    # Final cleanliness check after HEAD restore is clean.
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )

    def _preserve_committed_report(**kwargs: Any) -> None:
        # Intentionally leave the committed report file on disk; the real writer
        # would overwrite it with the AWF-synthesized satisfied report.
        del kwargs

    executor._write_satisfied_post_validation_conformance_report = _preserve_committed_report  # type: ignore[method-assign]
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

    executor._record_post_validation_conformance_event = record_event  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_post.md"),
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(
            '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'
        ),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # The HEAD restore reconciled the staged diff, so the committed report copy
    # remains on disk and the tree is clean.
    assert report_file.exists()
    assert report_file.read_text(encoding="utf-8") == committed_report
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source base-commit-sha --worktree --staged -- "
        ":(literal)docs/awf-plans/ws_post.conformance.json" in call
        for call in joined_calls
    )
    assert any(
        "restore --source HEAD --worktree --staged -- "
        ":(literal)docs/awf-plans/ws_post.conformance.json" in call
        for call in joined_calls
    )
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)


@pytest.mark.unit
async def test_satisfied_post_validation_conformance_report_unlinks_when_head_restore_fails(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    runner = _GitRestoreFakeRunner(worktree_path)
    report_path = Path("docs/awf-plans/ws_post.conformance.json")
    report_file = worktree_path / report_path
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        '{"status":"needs_iteration","summary":"stale miss","gaps":["fix me"]}',
        encoding="utf-8",
    )
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # before_compare
    runner.queue_result(returncode=0, stdout="fix-pass-head\n")  # before_compare_head
    runner.queue_result(returncode=0, stdout=f" M {report_path.as_posix()}\n")  # after_compare
    runner.queue_result(returncode=0, stdout="")  # committed paths since validated HEAD
    # base_commit restore leaves staged modification.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # Cleanliness check after base_commit restore still sees the staged path.
    runner.queue_result(returncode=0, stdout=f"M  {report_path.as_posix()}\n")
    # HEAD restore fails (or leaves the path dirty).
    runner.queue_result(returncode=128, stdout="", stderr="fatal: could not resolve HEAD\n")
    # Cleanliness check after unlink confirms the fallback removed the residue.
    runner.queue_result(returncode=0, stdout="")
    executor = _executor_with_runner(runner, tmp_path)
    executor._validation_run_evidence_for_conformance = AsyncMock(  # type: ignore[method-assign]
        return_value="VALIDATION_OK"
    )
    event_markers: list[str] = []

    async def record_event(**_kwargs: object) -> None:
        event_markers.append("record")

    executor._record_post_validation_conformance_event = record_event  # type: ignore[method-assign]
    profile = WorkspaceProfile.model_validate({"name": "planned", "planning": {"required": True}})
    handoff = _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary="AWF validation evidence is missing.",
            gaps=("Run AWF validation.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=Path("docs/awf-plans/ws_post.md"),
        report_path=report_path,
        iteration=0,
        max_iterations=2,
    )

    failure = await executor._run_post_validation_conformance_check(
        adapter=_PlanningAdapter(
            '{"status":"satisfied","summary":"validated evidence satisfies plan","gaps":[]}'
        ),  # type: ignore[arg-type]
        workspace=SimpleNamespace(id="ws_post", task_prompt="do it", task_tag=None),  # type: ignore[arg-type]
        profile=profile,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        worktree_path=worktree_path,
        model=None,
        handoff=handoff,
        validation_run_id="validation-run-1",
        base_commit="base-commit-sha",
    )

    assert failure is None
    assert event_markers == ["record"]
    # HEAD restore could not reconcile the index, so the report is unlinked.
    assert not report_file.exists()
    joined_calls = [" ".join(call.args) for call in runner.calls]
    assert any(
        "restore --source HEAD --worktree --staged -- "
        ":(literal)docs/awf-plans/ws_post.conformance.json" in call
        for call in joined_calls
    )
    # No staging or committing of the AWF artifact.
    assert all("add" not in call.args for call in runner.calls)
    assert all("commit" not in call.args for call in runner.calls)
