"""Automatic conformance salvage helper tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.service import conformance_salvage as salvage_mod
from awf.service.conformance_salvage import (
    CONFORMANCE_SALVAGE_POLICY_KEY,
    SALVAGE_BASE_UNAVAILABLE,
    SALVAGE_SOURCE_UNAVAILABLE,
    ConformanceSalvageCapture,
    ConformanceSalvageError,
    build_agent_timeout_salvage_retry_prompt,
    build_conformance_salvage_conflict_prompt,
    build_conformance_salvage_retry_prompt,
    capture_conformance_salvage,
    conformance_salvage_from_task_policy,
)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _seed_source_worktree(work_dir: Path, workspace_id: str) -> tuple[Path, str]:
    worktree = work_dir / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    _git(["init", "-q"], worktree)
    _git(["config", "user.name", "AWF Test"], worktree)
    _git(["config", "user.email", "awf@test.local"], worktree)
    (worktree / "src").mkdir()
    (worktree / "src/app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)
    (worktree / "src/app.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    return worktree, base_commit


@pytest.mark.unit
def test_run_git_marks_salvage_worktree_as_safe_directory(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    salvage_mod._run_git(  # noqa: SLF001
        tmp_path,
        ["status", "--short"],
        run=_run,
        env={},
    )

    assert calls
    assert "-c" in calls[0]
    assert f"safe.directory={tmp_path}" in calls[0]
    assert calls[0][-3:] == [str(tmp_path), "status", "--short"]


@pytest.mark.unit
def test_capture_reports_missing_base_commit_before_filesystem_lookup(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConformanceSalvageError) as exc_info:
        capture_conformance_salvage(
            work_dir=tmp_path,
            source_workspace_id="ws_missing_base",
            source_base_commit=None,
            conformance_evidence=None,
            conformance_evidence_ref=None,
            source_branch_name=None,
            source_remote_push_branch=None,
        )

    assert exc_info.value.reason_code == SALVAGE_BASE_UNAVAILABLE


@pytest.mark.unit
def test_capture_reports_missing_source_worktree(tmp_path: Path) -> None:
    with pytest.raises(ConformanceSalvageError) as exc_info:
        capture_conformance_salvage(
            work_dir=tmp_path,
            source_workspace_id="ws_missing",
            source_base_commit="a" * 40,
            conformance_evidence=None,
            conformance_evidence_ref=None,
            source_branch_name=None,
            source_remote_push_branch=None,
        )

    assert exc_info.value.reason_code == SALVAGE_SOURCE_UNAVAILABLE
    assert "worktree_path" in exc_info.value.detail


@pytest.mark.unit
def test_capture_reports_invalid_base_commit_from_git(tmp_path: Path) -> None:
    _seed_source_worktree(tmp_path, "ws_bad_base")

    with pytest.raises(ConformanceSalvageError) as exc_info:
        capture_conformance_salvage(
            work_dir=tmp_path,
            source_workspace_id="ws_bad_base",
            source_base_commit="b" * 40,
            conformance_evidence=None,
            conformance_evidence_ref=None,
            source_branch_name=None,
            source_remote_push_branch=None,
        )

    assert exc_info.value.reason_code == SALVAGE_BASE_UNAVAILABLE
    assert "stderr" in exc_info.value.detail


@pytest.mark.unit
def test_capture_policy_includes_optional_source_and_string_gap(tmp_path: Path) -> None:
    _, base_commit = _seed_source_worktree(tmp_path, "ws_capture")

    capture = capture_conformance_salvage(
        work_dir=tmp_path,
        source_workspace_id="ws_capture",
        source_base_commit=base_commit,
        conformance_evidence={
            "gaps": "add tests",
            "plan_path": "docs/awf-plans/ws_capture.md",
            "report_path": "docs/awf-plans/ws_capture.conformance.json",
        },
        conformance_evidence_ref={"source_workspace_id": "ws_capture"},
        source_branch_name="awf/ws_capture",
        source_remote_push_branch="awf/ws_capture",
    )

    policy = capture.as_policy()
    assert policy["source_branch_name"] == "awf/ws_capture"
    assert policy["source_remote_push_branch"] == "awf/ws_capture"
    assert policy["remaining_gaps"] == ["add tests"]
    assert policy["plan_path"] == "docs/awf-plans/ws_capture.md"
    assert policy["report_path"] == "docs/awf-plans/ws_capture.conformance.json"


@pytest.mark.unit
def test_capture_stages_tracked_files_under_ignored_parent_directory(tmp_path: Path) -> None:
    worktree = tmp_path / "git" / "worktrees" / "ws_ignored_parent"
    worktree.mkdir(parents=True)
    _git(["init", "-q"], worktree)
    _git(["config", "user.name", "AWF Test"], worktree)
    _git(["config", "user.email", "awf@test.local"], worktree)
    (worktree / ".gitignore").write_text("lib/\n", encoding="utf-8")
    (worktree / "apps/console/lib").mkdir(parents=True)
    (worktree / "apps/console/lib/format.ts").write_text(
        "export const value = 'old';\n",
        encoding="utf-8",
    )
    _git(["add", ".gitignore"], worktree)
    _git(["add", "-f", "apps/console/lib/format.ts"], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)

    (worktree / "apps/console/lib/format.ts").write_text(
        "export const value = 'new';\n",
        encoding="utf-8",
    )

    capture = capture_conformance_salvage(
        work_dir=tmp_path,
        source_workspace_id="ws_ignored_parent",
        source_base_commit=base_commit,
        conformance_evidence=None,
        conformance_evidence_ref=None,
        source_branch_name=None,
        source_remote_push_branch=None,
    )

    assert capture.implementation_paths == ["apps/console/lib/format.ts"]
    assert "export const value = 'new';" in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_capture_policy_omits_absent_optional_source_fields(tmp_path: Path) -> None:
    capture = ConformanceSalvageCapture(
        source_workspace_id="ws_source",
        source_base_commit="a" * 40,
        patch_path=tmp_path / "salvage.patch",
        patch_sha256="b" * 64,
        patch_bytes=42,
        implementation_paths=["src/app.py"],
        plan_artifact_paths=[],
        remaining_gaps=[],
        conformance_evidence_ref=None,
        source_branch_name=None,
        source_remote_push_branch=None,
        created_at="2026-05-03T00:00:00Z",
    )

    policy = capture.as_policy()

    assert "source_branch_name" not in policy
    assert "source_remote_push_branch" not in policy
    assert "plan_path" not in policy
    assert "report_path" not in policy


@pytest.mark.unit
def test_prompt_helpers_handle_long_or_missing_path_lists() -> None:
    long_paths = [f"src/path_{idx}.py" for idx in range(22)]

    retry_prompt = build_conformance_salvage_retry_prompt(
        task_prompt="finish task",
        evidence={"gaps": ["close gap"]},
        salvage={"implementation_paths": long_paths},
    )
    conflict_prompt = build_conformance_salvage_conflict_prompt(
        task_prompt="finish task",
        salvage={
            "implementation_paths": long_paths,
            "remaining_gaps": "legacy malformed gaps",
        },
        agent_patch_path=".awf/salvage/ws.patch",
        apply_error="",
    )
    missing_paths_prompt = build_conformance_salvage_conflict_prompt(
        task_prompt="finish task",
        salvage={"implementation_paths": SimpleNamespace()},
        agent_patch_path=".awf/salvage/ws.patch",
        apply_error="",
    )
    timeout_prompt = build_agent_timeout_salvage_retry_prompt(
        task_prompt="finish timed-out task",
        evidence={"reason_code": "AGENT_IDLE_TIMEOUT", "message": "no output"},
        salvage={"implementation_paths": long_paths},
    )

    assert "... and 2 more" in retry_prompt
    assert "... and 2 more" in conflict_prompt
    assert "... and 2 more" in timeout_prompt
    assert "Automatic AWF timeout salvage" in timeout_prompt
    assert "finish timed-out task" in timeout_prompt
    assert "Re-check conformance evidence." in conflict_prompt
    assert "No paths recorded." in missing_paths_prompt
    assert conformance_salvage_from_task_policy(None) is None
    assert conformance_salvage_from_task_policy({"other": {}}) is None
    assert conformance_salvage_from_task_policy(
        {CONFORMANCE_SALVAGE_POLICY_KEY: {"patch_path": "x"}}
    ) == {"patch_path": "x"}
