"""CI repair salvage helper tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.service import repair_salvage as repair_salvage_mod
from awf.service._git_salvage_utils import GIT_TIMEOUT_SECONDS
from awf.service.repair_salvage import (
    REPAIR_SALVAGE_BASE_UNAVAILABLE,
    REPAIR_SALVAGE_NO_DIFF,
    REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
    RepairSalvageError,
    as_repair_salvage_details,
    capture_ci_repair_salvage,
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


def _seed_worktree(work_dir: Path, workspace_id: str) -> tuple[Path, str]:
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
    return worktree, base_commit


@pytest.mark.unit
def test_capture_staged_only_tracked_changes(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_staged")
    worktree = tmp_path / "git" / "worktrees" / "ws_staged"
    (worktree / "src/app.py").write_text("VALUE = 'staged'\n", encoding="utf-8")
    _git(["add", "src/app.py"], worktree)

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_staged",
        operation_start_head=base_commit,
        operation_id="op_staged",
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py"]
    assert capture.patch_path.is_file()
    assert capture.patch_bytes > 0
    assert len(capture.patch_sha256) == 64


@pytest.mark.unit
def test_capture_unstaged_tracked_changes(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_unstaged")
    worktree = tmp_path / "git" / "worktrees" / "ws_unstaged"
    (worktree / "src/app.py").write_text("VALUE = 'unstaged'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_unstaged",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py"]
    assert "unstaged" in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_capture_untracked_files(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_untracked")
    worktree = tmp_path / "git" / "worktrees" / "ws_untracked"
    (worktree / "src/new.py").write_text("print('new')\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_untracked",
        operation_start_head=base_commit,
        operation_id="op_untracked",
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/new.py"]
    assert capture.patch_path.with_suffix(".json").is_file()


@pytest.mark.unit
def test_capture_includes_tracked_file_named_exactly_agent_memory(tmp_path: Path) -> None:
    """A tracked file spelled exactly ``.claude/agent-memory`` must be salvageable."""
    _, base_commit = _seed_worktree(tmp_path, "ws_exact_memory_file")
    worktree = tmp_path / "git" / "worktrees" / "ws_exact_memory_file"
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True)
    agent_memory_file = claude_dir / "agent-memory"
    agent_memory_file.write_text("tracked knowledge\n", encoding="utf-8")
    _git(["add", ".claude/agent-memory"], worktree)
    _git(["commit", "-q", "-m", "add agent-memory file"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)
    agent_memory_file.write_text("modified knowledge\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_exact_memory_file",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == [".claude/agent-memory"]
    assert "modified knowledge" in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_capture_excludes_agent_runtime_memory_paths(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_runtime")
    worktree = tmp_path / "git" / "worktrees" / "ws_runtime"
    (worktree / "src/app.py").write_text("VALUE = 'fix'\n", encoding="utf-8")
    memory_dir = worktree / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "notes.md").write_text("memory only\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_runtime",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py"]
    patch_text = capture.patch_path.read_text(encoding="utf-8")
    assert "agent-memory" not in patch_text


@pytest.mark.unit
def test_capture_excludes_awf_plan_artifacts(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_plan")
    worktree = tmp_path / "git" / "worktrees" / "ws_plan"
    (worktree / "src/app.py").write_text("VALUE = 'fix'\n", encoding="utf-8")
    plan_dir = worktree / "docs" / "awf-plans"
    plan_dir.mkdir(parents=True)
    (plan_dir / "ws_plan.md").write_text("# plan\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_plan",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py"]
    assert "awf-plans" not in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_run_git_timeout_raises_repair_salvage_error(tmp_path: Path) -> None:
    def _run_timeout(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, timeout=GIT_TIMEOUT_SECONDS)

    with pytest.raises(RepairSalvageError) as exc_info:
        repair_salvage_mod._run_git(  # noqa: SLF001
            tmp_path,
            ["status"],
            run=_run_timeout,
            env={},
            failure_reason=REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
        )

    assert exc_info.value.reason_code == REPAIR_SALVAGE_SOURCE_UNAVAILABLE
    assert "timed out" in str(exc_info.value)
    assert exc_info.value.detail["timeout_seconds"] == GIT_TIMEOUT_SECONDS


@pytest.mark.unit
def test_capture_missing_operation_start_head_raises(tmp_path: Path) -> None:
    with pytest.raises(RepairSalvageError) as exc_info:
        capture_ci_repair_salvage(
            work_dir=tmp_path,
            artifacts_root=tmp_path / "artifacts",
            workspace_id="ws_missing_base",
            operation_start_head=None,
            operation_id=None,
            operation_type="ci_repair",
            phase="ci_repair_commit_sink",
        )

    assert exc_info.value.reason_code == REPAIR_SALVAGE_BASE_UNAVAILABLE


@pytest.mark.unit
def test_capture_missing_worktree_raises(tmp_path: Path) -> None:
    with pytest.raises(RepairSalvageError) as exc_info:
        capture_ci_repair_salvage(
            work_dir=tmp_path,
            artifacts_root=tmp_path / "artifacts",
            workspace_id="ws_missing",
            operation_start_head="a" * 40,
            operation_id=None,
            operation_type="ci_repair",
            phase="ci_repair_commit_sink",
        )

    assert exc_info.value.reason_code == REPAIR_SALVAGE_SOURCE_UNAVAILABLE


@pytest.mark.unit
def test_capture_metadata_includes_digest_and_paths(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_meta")
    worktree = tmp_path / "git" / "worktrees" / "ws_meta"
    (worktree / "src/app.py").write_text("VALUE = 'meta'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        work_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_meta",
        operation_start_head=base_commit,
        operation_id="op_meta",
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    details = as_repair_salvage_details(capture)
    assert details["patch_path"] == str(capture.patch_path)
    assert details["patch_sha256"] == capture.patch_sha256
    assert details["patch_bytes"] == capture.patch_bytes
    assert details["affected_paths"] == ["src/app.py"]
    assert details["phase"] == "ci_repair_commit_sink"
    assert details["operation_type"] == "ci_repair"
    assert details["operation_id"] == "op_meta"
    assert details["operation_start_head"] == base_commit
    assert details["created_at"]


@pytest.mark.unit
def test_capture_only_agent_runtime_raises_no_diff(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_runtime_only")
    worktree = tmp_path / "git" / "worktrees" / "ws_runtime_only"
    memory_dir = worktree / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "notes.md").write_text("memory only\n", encoding="utf-8")

    with pytest.raises(RepairSalvageError) as exc_info:
        capture_ci_repair_salvage(
            work_dir=tmp_path,
            artifacts_root=tmp_path / "artifacts",
            workspace_id="ws_runtime_only",
            operation_start_head=base_commit,
            operation_id=None,
            operation_type="ci_repair",
            phase="ci_repair_commit_sink",
        )

    assert exc_info.value.reason_code == REPAIR_SALVAGE_NO_DIFF
