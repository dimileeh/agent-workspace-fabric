"""Source Git metadata safety coverage for NEEDS_HUMAN clarification re-asks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
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
