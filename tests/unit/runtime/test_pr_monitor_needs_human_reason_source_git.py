"""Source Git metadata safety coverage for NEEDS_HUMAN clarification re-asks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime import ownership
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git in a temporary repository."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_mirrored_worktree(
    tmp_path: Path,
    *,
    repository_name: str,
    worktree_name: str,
    tracked_contents: str,
    worktrees_root: Path | None = None,
) -> Path:
    """Create one AWF-shaped linked worktree backed by a bare mirror."""
    source = tmp_path / f"{repository_name}-source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "awf@example.com")
    _git(source, "config", "user.name", "AWF Test")
    (source / "tracked.txt").write_text(tracked_contents, encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-qm", "initial")

    mirror = tmp_path / "git" / "mirrors" / f"{repository_name}.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(mirror)],
        check=True,
        capture_output=True,
        text=True,
    )
    worktree = (worktrees_root or (tmp_path / "git" / "worktrees")) / worktree_name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree


@pytest.mark.unit
def test_validated_source_worktree_git_context_reads_control_files_from_pinned_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation must not reopen writable source Git metadata by pathname."""
    workspace_id = "ws_pinned_metadata_reads"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    expected_head_snapshot = (source_git_dir / "HEAD").read_text(encoding="utf-8")
    expected_resolved_head = _git(source, "rev-parse", "HEAD").stdout.strip()
    real_read_text = Path.read_text

    def _read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name in {".git", "commondir", "gitdir", "HEAD"}:
            raise AssertionError(f"path-based control-file read: {self}")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _read_text)

    context = ownership.validated_source_worktree_git_context(source, workspace_id)
    try:
        assert context.mirror_path == source_git_dir.parent.parent
        assert str(context.linked_git_dir).startswith("/proc/")
        assert context.head_snapshot == expected_head_snapshot
        assert context.resolved_head == expected_resolved_head
    finally:
        os.close(context.linked_git_dir_fd)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("head_snapshot", "command_result"),
    [
        ("not a commit\n", None),
        ("ref: refs/heads/missing\n", None),
        (
            "ref: refs/heads/main\n",
            subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="not a commit\n", stderr=""
            ),
        ),
    ],
)
def test_validated_source_worktree_git_context_rejects_unresolvable_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_snapshot: str,
    command_result: subprocess.CompletedProcess[str] | None,
) -> None:
    """Validation fails closed when it cannot retain a source commit ID."""
    workspace_id = "ws_unresolvable_head"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    (source_git_dir / "HEAD").write_text(head_snapshot, encoding="utf-8")
    if command_result is not None:
        monkeypatch.setattr(ownership.subprocess, "run", lambda *_args, **_kwargs: command_result)

    with pytest.raises(ValueError, match="source Git HEAD"):
        ownership.validated_source_worktree_git_context(source, workspace_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "resolution_error",
    [OSError("git unavailable"), subprocess.TimeoutExpired(cmd="git", timeout=30.0)],
)
def test_validated_source_worktree_git_context_rejects_head_resolution_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolution_error: OSError | subprocess.TimeoutExpired,
) -> None:
    """A failed source-Git resolver cannot leave an unpinned clarification ref."""
    workspace_id = "ws_head_resolution_error"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )

    def _raise_resolution_error(*_args: object, **_kwargs: object) -> None:
        raise resolution_error

    monkeypatch.setattr(ownership.subprocess, "run", _raise_resolution_error)

    with pytest.raises(ValueError, match="cannot resolve source Git HEAD"):
        ownership.validated_source_worktree_git_context(source, workspace_id)


@pytest.mark.unit
@pytest.mark.parametrize("control_file", [".git", "commondir", "gitdir", "HEAD"])
def test_validated_source_worktree_git_context_rejects_fifo_control_files(
    tmp_path: Path,
    control_file: str,
) -> None:
    """A FIFO in writable source Git metadata must fail closed without a read."""
    workspace_id = f"ws_fifo_{control_file.removeprefix('.')}"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    control_path = (
        source / control_file if control_file == ".git" else source_git_dir / control_file
    )
    control_path.unlink()
    os.mkfifo(control_path)

    with pytest.raises(ValueError, match="Git metadata"):
        ownership.validated_source_worktree_git_context(source, workspace_id)


@pytest.mark.unit
@pytest.mark.parametrize("control_file", [".git", "commondir", "gitdir", "HEAD"])
def test_validated_source_worktree_git_context_rejects_symlinked_control_files(
    tmp_path: Path,
    control_file: str,
) -> None:
    """A symlink cannot redirect a source Git control-file read."""
    workspace_id = f"ws_symlink_{control_file.removeprefix('.')}"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    control_path = (
        source / control_file if control_file == ".git" else source_git_dir / control_file
    )
    control_path.unlink()
    control_path.symlink_to(tmp_path / "replacement")

    with pytest.raises(ValueError, match="Git metadata"):
        ownership.validated_source_worktree_git_context(source, workspace_id)


@pytest.mark.unit
def test_validated_source_worktree_git_context_rejects_oversized_control_file(
    tmp_path: Path,
) -> None:
    """An oversized source Git control file must be rejected before it is read."""
    workspace_id = "ws_oversized_metadata"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    commondir = source_git_dir / "commondir"
    with commondir.open("wb") as file:
        file.truncate(1024 * 1024 + 1)

    with pytest.raises(ValueError, match="size limit"):
        ownership.validated_source_worktree_git_context(source, workspace_id)


@pytest.mark.unit
def test_validated_source_worktree_git_context_keeps_commondir_fallback(
    tmp_path: Path,
) -> None:
    """A missing commondir retains the established linked-metadata fallback."""
    workspace_id = "ws_missing_commondir"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    (source_git_dir / "commondir").unlink()

    context = ownership.validated_source_worktree_git_context(source, workspace_id)
    try:
        assert context.mirror_path == source_git_dir.parent.parent
    finally:
        os.close(context.linked_git_dir_fd)


@pytest.mark.unit
def test_validated_source_worktree_git_context_keeps_whitespace_commondir_fallback(
    tmp_path: Path,
) -> None:
    """Whitespace-only commondir uses the established mirror fallback."""
    workspace_id = "ws_whitespace_commondir"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    (source_git_dir / "commondir").write_text(" \t\n", encoding="utf-8")

    context = ownership.validated_source_worktree_git_context(source, workspace_id)
    try:
        assert context.mirror_path == source_git_dir.parent.parent
    finally:
        os.close(context.linked_git_dir_fd)


class _EnvLocalCommandRunner:
    """Run monitor Git commands while accepting its sanitized environment."""

    async def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run and normalize a Git command result."""
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


@pytest.mark.unit
async def test_reask_rejects_source_git_pointer_to_other_mirror_before_head_or_worktree_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification must not expose a second repository after source pointer tampering."""
    workspace_id = "ws_source_pointer"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    foreign = _init_mirrored_worktree(
        tmp_path,
        repository_name="foreign",
        worktree_name=workspace_id,
        tracked_contents="foreign repository\n",
        worktrees_root=tmp_path / "foreign-worktrees",
    )
    foreign_git_dir = (
        (foreign / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    (source / ".git").write_text(f"gitdir: {foreign_git_dir}\n", encoding="utf-8")
    (source / "tracked.txt").write_text("foreign repository\n", encoding="utf-8")

    reask_invocations: list[dict[str, object]] = []
    unavailable_reasons: list[str] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask_invocations.append(dict(kwargs))
        return VerdictResult(verdict="needs_human", reason="select a deployment region")

    async def _rev_parse_head(worktree_path: Path, *, timeout_seconds: float | None = None) -> str:
        del timeout_seconds
        return _git(worktree_path, "rev-parse", "HEAD").stdout.strip()

    async def _record_needs_human_reason_missing(_runner: object, **kwargs: object) -> None:
        unavailable_reasons.append(str(kwargs["reason_code"]))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_EnvLocalCommandRunner()),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_record_needs_human_reason_missing",
        _record_needs_human_reason_missing,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert reask_invocations == []
    assert unavailable_reasons == ["NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"]
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_reask_rejects_source_git_symlink_to_foreign_git_directory_before_head_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory-style Git entry cannot bypass pinned source-metadata validation."""
    workspace_id = "ws_source_git_directory_symlink"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    foreign = _init_mirrored_worktree(
        tmp_path,
        repository_name="foreign",
        worktree_name=workspace_id,
        tracked_contents="foreign repository\n",
        worktrees_root=tmp_path / "foreign-worktrees",
    )
    foreign_git_dir = Path(
        (foreign / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    (source / ".git").unlink()
    (source / ".git").symlink_to(foreign_git_dir, target_is_directory=True)

    reask_invocations: list[dict[str, object]] = []
    unavailable_reasons: list[str] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask_invocations.append(dict(kwargs))
        pytest.fail("a directory-style source Git entry must not trigger a re-ask")

    async def _unexpected_rev_parse_head(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an existing source Git entry must use validated metadata")

    async def _record_needs_human_reason_missing(_runner: object, **kwargs: object) -> None:
        unavailable_reasons.append(str(kwargs["reason_code"]))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_EnvLocalCommandRunner()),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_unexpected_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_record_needs_human_reason_missing",
        _record_needs_human_reason_missing,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert reask_invocations == []
    assert unavailable_reasons == ["NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"]
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_reask_rejects_source_mirror_alternates_before_snapshot_and_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification must not resolve a skip-worktree blob from another repository."""
    workspace_id = "ws_source_alternates"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    foreign = _init_mirrored_worktree(
        tmp_path,
        repository_name="foreign",
        worktree_name=workspace_id,
        tracked_contents="foreign repository\n",
        worktrees_root=tmp_path / "foreign-worktrees",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    source_mirror = source_git_dir.parent.parent
    foreign_git_dir = Path(
        (foreign / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    foreign_blob = _git(foreign, "rev-parse", "HEAD:tracked.txt").stdout.strip()
    source_head = _git(source, "rev-parse", "HEAD").stdout.strip()
    forged_tree = subprocess.run(
        ["git", "--git-dir", str(source_mirror), "mktree", "--missing"],
        check=True,
        capture_output=True,
        input=f"100644 blob {foreign_blob}\ttracked.txt\n",
        text=True,
    ).stdout.strip()
    forged_head = subprocess.run(
        [
            "git",
            "--git-dir",
            str(source_mirror),
            "-c",
            "user.email=awf@example.com",
            "-c",
            "user.name=AWF Test",
            "commit-tree",
            forged_tree,
            "-p",
            source_head,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    alternates_path = source_mirror / "objects" / "info" / "alternates"
    alternates_path.parent.mkdir(parents=True, exist_ok=True)
    alternates_path.write_text(f"{foreign_git_dir.parent.parent / 'objects'}\n", encoding="utf-8")
    _git(source, "update-ref", "HEAD", forged_head)
    _git(source, "read-tree", forged_head)
    _git(source, "update-index", "--skip-worktree", "tracked.txt")
    assert _git(source, "status", "--porcelain").stdout == ""

    reask_invocations: list[dict[str, object]] = []
    reask_contents: list[str] = []
    unavailable_reasons: list[str] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask_invocations.append(dict(kwargs))
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        reask_contents.append((reask / "tracked.txt").read_text(encoding="utf-8"))
        return VerdictResult(verdict="needs_human", reason="select a deployment region")

    async def _unexpected_rev_parse_head(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a linked source must resolve HEAD through its pinned admin directory")

    async def _record_needs_human_reason_missing(_runner: object, **kwargs: object) -> None:
        unavailable_reasons.append(str(kwargs["reason_code"]))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_EnvLocalCommandRunner()),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_unexpected_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_record_needs_human_reason_missing",
        _record_needs_human_reason_missing,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert reask_invocations == []
    assert reask_contents == []
    assert unavailable_reasons == ["NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"]
    assert alternates_path.exists()
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_reask_uses_validated_source_git_context_for_head_and_worktree_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid source pointer is pinned instead of re-read for clarification Git commands."""
    workspace_id = "ws_pinned_source"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    reask_contents: list[str] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        reask_contents.append((reask / "tracked.txt").read_text(encoding="utf-8"))
        return VerdictResult(verdict="needs_human", reason="select a deployment region")

    async def _unexpected_rev_parse_head(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a linked source must resolve HEAD through its pinned admin directory")

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_EnvLocalCommandRunner()),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_unexpected_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human", reason="select a deployment region")
    assert reask_contents == ["source repository\n"]
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_reask_uses_validated_head_commit_when_symbolic_ref_changes_transiently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored symbolic source ref cannot redirect the clarification checkout."""
    workspace_id = "ws_head_snapshot"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    mirror = source_git_dir.parent.parent
    source_head = _git(source, "rev-parse", "HEAD").stdout.strip()
    source_ref = f"refs/heads/awf/{workspace_id}"
    subprocess.run(
        ["git", "--git-dir", str(mirror), "update-ref", source_ref, source_head],
        check=True,
        capture_output=True,
        text=True,
    )
    (source_git_dir / "HEAD").write_text(f"ref: {source_ref}\n", encoding="utf-8")
    expected_head_snapshot = (source_git_dir / "HEAD").read_text(encoding="utf-8")
    attacker = tmp_path / "attacker"
    subprocess.run(
        ["git", "clone", "-q", str(mirror), str(attacker)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(attacker, "config", "user.email", "awf@example.com")
    _git(attacker, "config", "user.name", "AWF Test")
    (attacker / "tracked.txt").write_text("attacker repository\n", encoding="utf-8")
    _git(attacker, "add", "tracked.txt")
    _git(attacker, "commit", "-qm", "attacker")
    attacker_head = _git(attacker, "rev-parse", "HEAD").stdout.strip()
    subprocess.run(
        ["git", "-C", str(attacker), "push", "-q", "origin", "HEAD:refs/heads/attacker"],
        check=True,
        capture_output=True,
        text=True,
    )

    reask_contents: list[str] = []

    class _HeadMutatingRunner(_EnvLocalCommandRunner):
        """Move then restore the captured symbolic ref during the re-ask."""

        def __init__(self) -> None:
            self.mutated = False

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            if not self.mutated and "rev-parse" in args:
                self.mutated = True
                subprocess.run(
                    ["git", "--git-dir", str(mirror), "update-ref", source_ref, attacker_head],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                try:
                    return await super().run(args, timeout_seconds=timeout_seconds, env=env)
                finally:
                    subprocess.run(
                        ["git", "--git-dir", str(mirror), "update-ref", source_ref, source_head],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        reask_contents.append((reask / "tracked.txt").read_text(encoding="utf-8"))
        return VerdictResult(verdict="needs_human", reason="select a deployment region")

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_HeadMutatingRunner()),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
    )
    monkeypatch.setattr(
        comments,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human", reason="select a deployment region")
    assert reask_contents == ["source repository\n"]
    assert (source_git_dir / "HEAD").read_text(encoding="utf-8") == expected_head_snapshot
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == source_head
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_reask_does_not_fall_back_to_primary_checkout_when_source_git_file_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validated source must not use the unisolated test-double re-ask seam."""
    workspace_id = "ws_missing_source_git_file"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    reask_invocations: list[dict[str, object]] = []
    unavailable_reasons: list[str] = []

    class _SourceGitFileRemovingRunner(_EnvLocalCommandRunner):
        """Remove the writable source control file after snapshot resolution."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            if "rev-parse" in args and "--verify" in args:
                (source / ".git").unlink()
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask_invocations.append(dict(kwargs))
        pytest.fail("a lost real Git control file must not trigger an unisolated re-ask")

    async def _record_needs_human_reason_missing(_runner: object, **kwargs: object) -> None:
        unavailable_reasons.append(str(kwargs["reason_code"]))

    async def _unexpected_rev_parse_head(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a linked source must resolve HEAD through its pinned admin directory")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_SourceGitFileRemovingRunner()),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_unexpected_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_record_needs_human_reason_missing",
        _record_needs_human_reason_missing,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert result == VerdictResult(verdict="needs_human")
    assert reask_invocations == []
    assert unavailable_reasons == ["NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE"]
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_reask_pins_validated_source_admin_directory_through_head_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing source admin metadata cannot redirect the clarification revision."""
    workspace_id = "ws_pinned_admin_directory"
    source = _init_mirrored_worktree(
        tmp_path,
        repository_name="source",
        worktree_name=workspace_id,
        tracked_contents="source repository\n",
    )
    source_git_dir = Path(
        (source / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    mirror = source_git_dir.parent.parent
    foreign_ref = "refs/heads/foreign"
    foreign_source = tmp_path / "foreign-source"
    subprocess.run(
        ["git", "clone", "-q", str(mirror), str(foreign_source)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(foreign_source, "config", "user.email", "awf@example.com")
    _git(foreign_source, "config", "user.name", "AWF Test")
    (foreign_source / "tracked.txt").write_text("foreign repository\n", encoding="utf-8")
    _git(foreign_source, "add", "tracked.txt")
    _git(foreign_source, "commit", "-qm", "foreign")
    subprocess.run(
        ["git", "-C", str(foreign_source), "push", "-q", "origin", f"HEAD:{foreign_ref}"],
        check=True,
        capture_output=True,
        text=True,
    )
    foreign = source.parent / "foreign"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            "--detach",
            str(foreign),
            foreign_ref,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    foreign_git_dir = Path(
        (foreign / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    reask_contents: list[str] = []

    class _AdminDirectorySwappingRunner(_EnvLocalCommandRunner):
        """Swap the source admin path exactly before its HEAD command starts."""

        def __init__(self) -> None:
            self.swapped = False
            self.pinned_source_git_dir: Path | None = None

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            if not self.swapped and args[-2:] == ["rev-parse", "HEAD"]:
                self.swapped = True
                self.pinned_source_git_dir = Path(args[2])
                source_git_dir.rename(source_git_dir.with_name(f"{workspace_id}-original"))
                source_git_dir.symlink_to(foreign_git_dir, target_is_directory=True)
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        reask_contents.append((reask / "tracked.txt").read_text(encoding="utf-8"))
        return VerdictResult(verdict="needs_human", reason="select a deployment region")

    async def _unexpected_rev_parse_head(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a linked source must resolve HEAD through its pinned admin directory")

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return

    command_runner = _AdminDirectorySwappingRunner()
    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=command_runner),
        _worktrees_root=source.parent,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_unexpected_rev_parse_head,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )
    monkeypatch.setattr(
        comments,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    result = await comments._enforce_needs_human_reason(
        runner,
        result=VerdictResult(verdict="needs_human"),
        original_prompt="original review task",
        workspace_id=workspace_id,
        pr_number=1,
        item_id="thread_1",
        item_kind="thread",
        item_author=None,
        item_path=None,
        item_line=None,
        commit_message="fix: address thread_1",
        compose_project="project",
        compose_file=tmp_path / "compose.yml",
        state=None,
        task_tag=None,
        operation_start_head=None,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    assert command_runner.swapped
    assert command_runner.pinned_source_git_dir is not None
    assert str(command_runner.pinned_source_git_dir).startswith("/proc/")
    assert result == VerdictResult(verdict="needs_human", reason="select a deployment region")
    assert reask_contents == ["source repository\n"]
    assert not command_runner.pinned_source_git_dir.exists()
    assert not list(source.parent.glob(f"{workspace_id}__companion__isolated_reask_*"))
