"""Hosted validation fix-pass terminal-head synchronization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.common.git_identity import git_safe_directory_config_args
from awf.node.git_manager import git_env_without_object_lookup_overrides


def _hosted_identity_str(identity: Mapping[str, Any] | None, key: str) -> str | None:
    value = identity.get(key) if identity is not None else None
    return value if isinstance(value, str) and value.strip() else None


def _hosted_agent_error_terminal_head_sha(exc: AgentRunError) -> str | None:
    value = getattr(exc.result, "terminal_head_sha", None)
    if isinstance(value, str) and value.strip():
        return value
    return _hosted_identity_str(exc.details, "terminal_head_sha")


async def _sync_hosted_validation_fix_head(
    self: Any,
    *,
    worktree_path: Path,
    hosted_pr_identity: Mapping[str, Any] | None,
    terminal_head_sha: str,
) -> CommandResult:
    repo_url = _hosted_identity_str(hosted_pr_identity, "head_repo_url") or _hosted_identity_str(
        hosted_pr_identity,
        "repo_url",
    )
    head_ref = _hosted_identity_str(hosted_pr_identity, "head_ref")
    if repo_url is None or head_ref is None:
        return CommandResult(
            returncode=1,
            stdout="",
            stderr="hosted validation fix missing remote PR head identity",
            reason_code="HOSTED_REMOTE_HEAD_IDENTITY_MISSING",
        )

    git_env = git_env_without_object_lookup_overrides()
    fetch = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            "--no-tags",
            repo_url,
            head_ref,
        ],
        env=git_env,
    )
    if not fetch.ok:
        return CommandResult(
            returncode=fetch.returncode,
            stdout=fetch.stdout,
            stderr=fetch.stderr,
            reason_code=fetch.reason_code or "HOSTED_REMOTE_HEAD_FETCH_FAILED",
        )

    rev_parse = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "FETCH_HEAD",
        ],
        env=git_env,
    )
    fetched_sha = rev_parse.stdout.strip()
    if not rev_parse.ok or fetched_sha.lower() != terminal_head_sha.lower():
        return CommandResult(
            returncode=rev_parse.returncode if rev_parse.returncode != 0 else 1,
            stdout=rev_parse.stdout,
            stderr=(
                "hosted validation fix terminal head mismatch: "
                f"reported {terminal_head_sha}, fetched {fetched_sha or '<unknown>'}"
            ),
            reason_code="HOSTED_REMOTE_HEAD_MISMATCH",
        )

    reset = await self._runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "reset",
            "--hard",
            fetched_sha,
        ],
        env=git_env,
    )
    if not reset.ok:
        return CommandResult(
            returncode=reset.returncode,
            stdout=reset.stdout,
            stderr=reset.stderr,
            reason_code=reset.reason_code or "HOSTED_REMOTE_HEAD_SYNC_FAILED",
        )

    return CommandResult(returncode=0, stdout=fetched_sha, stderr="")
