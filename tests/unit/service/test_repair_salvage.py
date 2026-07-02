"""CI repair salvage helper tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awf.service import repair_salvage as repair_salvage_mod
from awf.service._git_salvage_utils import (
    GIT_TIMEOUT_SECONDS,
    paths_from_ls_files_z,
    paths_from_name_status,
)
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
def test_capture_uses_worktrees_root_not_work_dir_layout(tmp_path: Path) -> None:
    """Salvage must resolve the worktree via worktrees_root, not work_dir/git/worktrees."""
    custom_root = tmp_path / "custom-worktrees"
    workspace_id = "ws_custom_root"
    worktree = custom_root / workspace_id
    worktree.mkdir(parents=True)
    _git(["init", "-q"], worktree)
    _git(["config", "user.name", "AWF Test"], worktree)
    _git(["config", "user.email", "awf@test.local"], worktree)
    (worktree / "src").mkdir()
    (worktree / "src/app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    _git(["add", "."], worktree)
    _git(["commit", "-q", "-m", "base"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)
    (worktree / "src/app.py").write_text("VALUE = 'custom'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=custom_root,
        artifacts_root=tmp_path / "artifacts",
        workspace_id=workspace_id,
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py"]
    assert "custom" in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_capture_staged_only_tracked_changes(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_staged")
    worktree = tmp_path / "git" / "worktrees" / "ws_staged"
    (worktree / "src/app.py").write_text("VALUE = 'staged'\n", encoding="utf-8")
    _git(["add", "src/app.py"], worktree)

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
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
        worktrees_root=tmp_path / "git" / "worktrees",
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
        worktrees_root=tmp_path / "git" / "worktrees",
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
        worktrees_root=tmp_path / "git" / "worktrees",
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
def test_capture_includes_tracked_agent_memory_descendant(tmp_path: Path) -> None:
    """Tracked modifications under ``.claude/agent-memory/`` must be salvageable."""
    _, base_commit = _seed_worktree(tmp_path, "ws_tracked_memory")
    worktree = tmp_path / "git" / "worktrees" / "ws_tracked_memory"
    memory_dir = worktree / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True)
    notes = memory_dir / "notes.md"
    notes.write_text("original knowledge\n", encoding="utf-8")
    _git(["add", ".claude/agent-memory/notes.md"], worktree)
    _git(["commit", "-q", "-m", "add tracked memory"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)
    notes.write_text("modified knowledge\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_tracked_memory",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == [".claude/agent-memory/notes.md"]
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
        worktrees_root=tmp_path / "git" / "worktrees",
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
        worktrees_root=tmp_path / "git" / "worktrees",
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
def test_run_git_timeout_decodes_byte_output_for_json_safe_detail(tmp_path: Path) -> None:
    def _run_timeout(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            args,
            timeout=GIT_TIMEOUT_SECONDS,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    with pytest.raises(RepairSalvageError) as exc_info:
        repair_salvage_mod._run_git(  # noqa: SLF001
            tmp_path,
            ["status"],
            run=_run_timeout,
            env={},
            failure_reason=REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
        )

    detail = exc_info.value.detail
    assert detail["stdout"] == "partial stdout"
    assert detail["stderr"] == "partial stderr"
    json.dumps(detail)


@pytest.mark.unit
def test_capture_missing_operation_start_head_raises(tmp_path: Path) -> None:
    with pytest.raises(RepairSalvageError) as exc_info:
        capture_ci_repair_salvage(
            worktrees_root=tmp_path / "git" / "worktrees",
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
            worktrees_root=tmp_path / "git" / "worktrees",
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
        worktrees_root=tmp_path / "git" / "worktrees",
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
    assert details["salvage_diff_base"] == base_commit
    assert details["created_at"]


@pytest.mark.unit
def test_paths_from_ls_files_z_preserves_tab_and_trailing_space_paths() -> None:
    """NUL-delimited ls-files parsing must preserve exact untracked path names."""
    assert paths_from_ls_files_z("src/foo\tbar.py\0") == ["src/foo\tbar.py"]
    assert paths_from_ls_files_z("src/trailing.py \0") == ["src/trailing.py "]
    assert paths_from_ls_files_z(".claude/agent-memory/note\t.md\0src/app.py\0") == [
        ".claude/agent-memory/note\t.md",
        "src/app.py",
    ]


@pytest.mark.unit
def test_capture_excludes_untracked_agent_memory_with_tab_in_filename(
    tmp_path: Path,
) -> None:
    """Untracked runtime memory with tabbed filenames must not leak into salvage."""
    _, base_commit = _seed_worktree(tmp_path, "ws_runtime_tab")
    worktree = tmp_path / "git" / "worktrees" / "ws_runtime_tab"
    (worktree / "src/app.py").write_text("VALUE = 'fix'\n", encoding="utf-8")
    memory_dir = worktree / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True)
    tab_memory = memory_dir / "note\tfile.md"
    tab_memory.write_text("memory only\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_runtime_tab",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py"]
    patch_text = capture.patch_path.read_text(encoding="utf-8")
    assert "agent-memory" not in patch_text


@pytest.mark.unit
def test_paths_from_name_status_z_preserves_tab_and_trailing_space_paths() -> None:
    """NUL-delimited parsing must not strip or split paths with tabs or trailing spaces."""
    assert paths_from_name_status("M\0src/foo\tbar.py\0") == ["src/foo\tbar.py"]
    assert paths_from_name_status("M\0src/trailing.py \0") == ["src/trailing.py "]
    assert paths_from_name_status("R100\0src/old\tname.py\0src/new\tname.py\0") == [
        "src/new\tname.py",
        "src/old\tname.py",
    ]


@pytest.mark.unit
def test_capture_preserves_path_with_tab_in_filename(tmp_path: Path) -> None:
    """Salvage must preserve exact paths when filenames contain tab characters."""
    _, base_commit = _seed_worktree(tmp_path, "ws_tab")
    worktree = tmp_path / "git" / "worktrees" / "ws_tab"
    tab_path = worktree / "src" / "foo\tbar.py"
    tab_path.parent.mkdir(parents=True, exist_ok=True)
    tab_path.write_text("VALUE = 'tab'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_tab",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/foo\tbar.py"]
    assert capture.patch_bytes > 0
    assert "tab" in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_capture_preserves_pathspec_magic_filename(tmp_path: Path) -> None:
    """Salvage must treat pathspec-magic filenames literally when generating the patch."""
    _, base_commit = _seed_worktree(tmp_path, "ws_pathspec_magic")
    worktree = tmp_path / "git" / "worktrees" / "ws_pathspec_magic"
    magic_path = worktree / ":(glob)foo.txt"
    magic_path.write_text("VALUE = 'magic'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_pathspec_magic",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == [":(glob)foo.txt"]
    assert capture.patch_bytes > 0
    assert "magic" in capture.patch_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_capture_renamed_tracked_file_includes_source_and_dest_paths(
    tmp_path: Path,
) -> None:
    """Renamed tracked files must salvage both source and destination paths."""
    _, base_commit = _seed_worktree(tmp_path, "ws_rename")
    worktree = tmp_path / "git" / "worktrees" / "ws_rename"
    _git(["mv", "src/app.py", "src/renamed.py"], worktree)

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_rename",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.affected_paths == ["src/app.py", "src/renamed.py"]
    patch_text = capture.patch_path.read_text(encoding="utf-8")
    assert "rename from src/app.py" in patch_text
    assert "rename to src/renamed.py" in patch_text
    assert "new file mode" not in patch_text


@pytest.mark.unit
def test_capture_preserves_crlf_line_endings_in_binary_patch(tmp_path: Path) -> None:
    """Salvage patches must preserve CRLF bytes for git apply against CRLF bases."""
    _, base_commit = _seed_worktree(tmp_path, "ws_crlf")
    worktree = tmp_path / "git" / "worktrees" / "ws_crlf"
    _git(["config", "core.autocrlf", "false"], worktree)
    crlf_file = worktree / "src/app.py"
    crlf_file.write_bytes(b"VALUE = 'old'\r\n")
    _git(["add", "src/app.py"], worktree)
    _git(["commit", "-q", "-m", "crlf base"], worktree)
    base_commit = _git(["rev-parse", "HEAD"], worktree)
    crlf_file.write_bytes(b"VALUE = 'new'\r\n")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_crlf",
        operation_start_head=base_commit,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    patch_bytes = capture.patch_path.read_bytes()
    assert b"+VALUE = 'new'\r\n" in patch_bytes
    assert b"+VALUE = 'new'\n" not in patch_bytes

    expected = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "diff",
            "--binary",
            base_commit,
            "--",
            "src/app.py",
        ],
        check=True,
        capture_output=True,
        text=False,
    ).stdout
    assert b"+VALUE = 'new'\r\n" in expected


@pytest.mark.unit
def test_run_git_bytes_timeout_raises_repair_salvage_error(tmp_path: Path) -> None:
    def _run_timeout(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(args, timeout=GIT_TIMEOUT_SECONDS)

    with pytest.raises(RepairSalvageError) as exc_info:
        repair_salvage_mod._run_git_bytes(  # noqa: SLF001
            tmp_path,
            ["diff", "--binary", "HEAD"],
            run=_run_timeout,
            env={},
            failure_reason=REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
        )

    assert exc_info.value.reason_code == REPAIR_SALVAGE_SOURCE_UNAVAILABLE
    assert "timed out" in str(exc_info.value)


@pytest.mark.unit
def test_capture_only_agent_runtime_raises_no_diff(tmp_path: Path) -> None:
    _, base_commit = _seed_worktree(tmp_path, "ws_runtime_only")
    worktree = tmp_path / "git" / "worktrees" / "ws_runtime_only"
    memory_dir = worktree / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "notes.md").write_text("memory only\n", encoding="utf-8")

    with pytest.raises(RepairSalvageError) as exc_info:
        capture_ci_repair_salvage(
            worktrees_root=tmp_path / "git" / "worktrees",
            artifacts_root=tmp_path / "artifacts",
            workspace_id="ws_runtime_only",
            operation_start_head=base_commit,
            operation_id=None,
            operation_type="ci_repair",
            phase="ci_repair_commit_sink",
        )

    assert exc_info.value.reason_code == REPAIR_SALVAGE_NO_DIFF


@pytest.mark.unit
def test_capture_salvage_diff_base_captures_residue_only_after_self_commit(
    tmp_path: Path,
) -> None:
    """Salvage against rollback anchor must exclude already-committed repair work."""
    _, operation_start_head = _seed_worktree(tmp_path, "ws_residue")
    worktree = tmp_path / "git" / "worktrees" / "ws_residue"
    (worktree / "src/app.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
    _git(["add", "src/app.py"], worktree)
    _git(["commit", "-q", "-m", "agent self-commit"], worktree)
    rollback_anchor = _git(["rev-parse", "HEAD"], worktree)
    (worktree / "src/app.py").write_text("VALUE = 'residue'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_residue",
        operation_start_head=operation_start_head,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
        salvage_diff_base=rollback_anchor,
    )

    assert capture.salvage_diff_base == rollback_anchor
    assert capture.affected_paths == ["src/app.py"]
    patch_text = capture.patch_path.read_text(encoding="utf-8")
    assert "residue" in patch_text
    assert "-VALUE = 'old'" not in patch_text

    _git(["reset", "--hard", rollback_anchor], worktree)
    subprocess.run(
        ["git", "-C", str(worktree), "apply", str(capture.patch_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (worktree / "src/app.py").read_text(encoding="utf-8") == "VALUE = 'residue'\n"


@pytest.mark.unit
def test_capture_operation_start_head_includes_self_commits_when_no_salvage_diff_base(
    tmp_path: Path,
) -> None:
    """Without a rollback anchor, salvage still diffs from operation_start_head."""
    _, operation_start_head = _seed_worktree(tmp_path, "ws_full")
    worktree = tmp_path / "git" / "worktrees" / "ws_full"
    (worktree / "src/app.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
    _git(["add", "src/app.py"], worktree)
    _git(["commit", "-q", "-m", "agent self-commit"], worktree)
    (worktree / "src/app.py").write_text("VALUE = 'residue'\n", encoding="utf-8")

    capture = capture_ci_repair_salvage(
        worktrees_root=tmp_path / "git" / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        workspace_id="ws_full",
        operation_start_head=operation_start_head,
        operation_id=None,
        operation_type="ci_repair",
        phase="ci_repair_commit_sink",
    )

    assert capture.salvage_diff_base == operation_start_head
    patch_text = capture.patch_path.read_text(encoding="utf-8")
    assert "-VALUE = 'old'" in patch_text
    assert "residue" in patch_text
