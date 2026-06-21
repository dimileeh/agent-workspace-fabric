"""Shared Git loaders for protected quality-gate file diffs."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.git_identity import git_safe_directory_config_args
from awf.control.quality_gates import (
    ProtectedFileDiff,
    diff_classified_protected_paths,
)
from awf.node.git_manager import git_env_without_object_lookup_overrides


def changed_paths_from_name_status_z(diff_stdout: str) -> tuple[str, ...]:
    """Extract all changed paths from ``git diff --name-status -z`` output."""
    if not diff_stdout:
        return ()
    if "\0" not in diff_stdout:
        raise ValueError("expected NUL-delimited output from `git diff --name-status -z`")

    fields = diff_stdout.split("\0")
    if fields[-1] != "":
        raise ValueError("truncated `--name-status -z` output: missing terminating NUL")
    fields = fields[:-1]

    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            raise ValueError("malformed `--name-status -z` output: empty status field")
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise ValueError(f"truncated `--name-status -z` record for status {status!r}")
        for _ in range(path_count):
            path = fields[index]
            index += 1
            if not path:
                raise ValueError(f"malformed `--name-status -z` record for status {status!r}")
            paths.append(path)
    return tuple(dict.fromkeys(paths))


async def committed_changed_paths_since(
    runner: AsyncCommandRunner,
    *,
    worktree_path: Path,
    base_ref: str,
) -> tuple[str, ...]:
    """Return committed paths changed since `base_ref`, including rename sources."""
    result = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "diff",
            "--name-status",
            "-z",
            f"{base_ref}..HEAD",
            "--",
        ],
        env=git_env_without_object_lookup_overrides(),
    )
    if not result.ok:
        raise RuntimeError(
            f"git diff --name-status -z failed while checking committed paths: {result.stderr}"
        )
    try:
        return changed_paths_from_name_status_z(result.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"git diff --name-status -z produced malformed committed-path output: {exc}"
        ) from exc


async def git_show_text(
    runner: AsyncCommandRunner,
    *,
    worktree_path: Path,
    refspec: str,
) -> str | None:
    """Return `git show` text, treating missing paths as absent content."""
    exists_result = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "cat-file",
            "-e",
            refspec,
        ],
        env=git_env_without_object_lookup_overrides(),
    )
    if not exists_result.ok:
        if await _git_refspec_missing_path_is_recoverable(
            runner,
            worktree_path=worktree_path,
            refspec=refspec,
        ):
            return None
        raise RuntimeError(
            f"git cat-file -e failed for {refspec!r} in {worktree_path}: "
            f"{_git_error_details(exists_result)}"
        )

    result = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "show",
            refspec,
        ],
        env=git_env_without_object_lookup_overrides(),
    )
    if result.ok:
        return result.stdout
    raise RuntimeError(
        f"git show failed for {refspec!r} in {worktree_path}: {_git_error_details(result)}"
    )


async def _git_refspec_missing_path_is_recoverable(
    runner: AsyncCommandRunner,
    *,
    worktree_path: Path,
    refspec: str,
) -> bool:
    base_ref, separator, path = refspec.partition(":")
    if not separator or not path:
        return False
    if not base_ref:
        result = await runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "ls-files",
                "--stage",
                "-z",
                "--",
                _git_literal_pathspec(path),
            ],
            env=git_env_without_object_lookup_overrides(),
        )
        return result.ok and not _git_z_listing_contains_path(result.stdout, path)

    result = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "ls-tree",
            "-z",
            base_ref,
            "--",
            _git_literal_pathspec(path),
        ],
        env=git_env_without_object_lookup_overrides(),
    )
    return result.ok and not _git_z_listing_contains_path(result.stdout, path)


def _git_literal_pathspec(path: str) -> str:
    return f":(literal){path}"


def _git_z_listing_contains_path(stdout: str, path: str) -> bool:
    for record in stdout.split("\0"):
        if not record:
            continue
        _metadata, separator, record_path = record.partition("\t")
        if not separator or record_path == path:
            return True
    return False


def _git_error_details(result: CommandResult) -> str:
    return (result.stderr or result.stdout or "<no output>").strip()


async def protected_file_diffs_for_committed_paths(
    runner: AsyncCommandRunner,
    *,
    worktree_path: Path,
    base_ref: str,
    changed_paths: Sequence[str],
    owned_paths: Sequence[str] = (),
) -> dict[str, ProtectedFileDiff]:
    """Load old/new content for committed protected files changed since `base_ref`."""
    diffs: dict[str, ProtectedFileDiff] = {}
    for path in diff_classified_protected_paths(changed_paths, owned_paths=owned_paths):
        old_text = await git_show_text(
            runner,
            worktree_path=worktree_path,
            refspec=f"{base_ref}:{path}",
        )
        new_text = await git_show_text(
            runner,
            worktree_path=worktree_path,
            refspec=f"HEAD:{path}",
        )
        diffs[path] = ProtectedFileDiff(
            path=path,
            old_text=old_text,
            new_text=new_text,
        )
    return diffs
