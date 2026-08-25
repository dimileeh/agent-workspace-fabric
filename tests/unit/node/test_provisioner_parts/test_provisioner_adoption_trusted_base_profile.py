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
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.db.models import Workspace
from awf.node import git_manager_detached as git_manager_detached_mod
from awf.node.git_manager import GitManager, GitOperationError
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
from awf.profiles.resolver import resolve_workspace_profile
from tests.unit.node.test_provisioner_parts._adoption_trusted_base_helpers import (
    _assert_trusted_base_stamp,
    _build_stale_head_safe_base_origin,
    _git,
    _git_stdout,
    _write_repo_profile,
    git_manager,
)

pytestmark = pytest.mark.unit

__all__ = ["git_manager"]


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
async def test_detached_worktree_rejects_committed_symlink_profile_marker(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Committed symlink markers must fail closed under ``--no-checkout``.

    Regression for PRRT_kwDOSJAM6s6cJD5W: returning ``None`` for mode ``120000``
    looks like an absent marker when the snapshot has no disk symlink, so
    resolution silently auto-detects or falls back to generic instead of
    loading or rejecting the configured profile.
    """
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    awf_dir = repo / ".awf"
    awf_dir.mkdir(parents=True)
    (awf_dir / "real-profile.yml").write_text(
        "name: linked-profile\ndocker:\n  mode: none\n",
        encoding="utf-8",
    )
    (awf_dir / "workspace.yml").symlink_to("real-profile.yml")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "symlink profile marker"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    ls_tree = _git_stdout(["ls-tree", "HEAD", ".awf/workspace.yml"], repo)
    assert ls_tree.startswith("120000")

    snap_id = "ws_symlink_marker__trusted_base_profile"
    with pytest.raises(GitOperationError) as exc_info:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id=snap_id,
            repo_url=str(repo),
            commit_sha=base_sha,
        )
    assert exc_info.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "symlink" in exc_info.value.stderr
    assert not git_manager._worktree_path_for(snap_id).exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
async def test_detached_worktree_rejects_symlinked_profile_parent_component(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Symlinked ``.awf`` parents must fail closed under ``--no-checkout``.

    Regression for PRRT_kwDOSJAM6s6cJT9x: leaf symlink markers already raise,
    but an intermediate ``.awf`` symlink (mode ``120000``) returned ``None``
    and looked absent, so resolution silently auto-detected / fell back to
    generic instead of loading or rejecting the configured profile.
    """
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    profile_dir = repo / "checked-in-profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "workspace.yml").write_text(
        "name: linked-parent-profile\ndocker:\n  mode: none\n",
        encoding="utf-8",
    )
    (repo / ".awf").symlink_to("checked-in-profile")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "symlink .awf parent"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    ls_tree = _git_stdout(["ls-tree", "HEAD", ".awf"], repo)
    assert ls_tree.startswith("120000")

    snap_id = "ws_symlink_parent__trusted_base_profile"
    with pytest.raises(GitOperationError) as exc_info:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id=snap_id,
            repo_url=str(repo),
            commit_sha=base_sha,
        )
    assert exc_info.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "symlink" in exc_info.value.stderr
    assert not git_manager._worktree_path_for(snap_id).exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
async def test_detached_worktree_skips_symlinked_autodetect_probe(
    git_manager: GitManager, tmp_path: Path
) -> None:
    """Symlinked probe paths must not abort a trusted-base snapshot.

    Regression for Bugbot 3854821069 / review 5021086934: fail-closed
    symlink/gitlink handling in ``_raw_commit_blob_bytes`` also ran for
    autodetect probes, so a symlinked ``package.json`` aborted provisioning
    even after a valid profile marker blob was published.
    """
    repo = tmp_path / "origin"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    awf_dir = repo / ".awf"
    awf_dir.mkdir(parents=True)
    (awf_dir / "workspace.yml").write_text(
        "name: adopted-from-marker\ndocker:\n  mode: none\n",
        encoding="utf-8",
    )
    (repo / "real-package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    (repo / "package.json").symlink_to("real-package.json")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "marker plus symlink probe"], repo)
    base_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    ls_tree = _git_stdout(["ls-tree", "HEAD", "package.json"], repo)
    assert ls_tree.startswith("120000")

    snap_id = "ws_symlink_probe__trusted_base_profile"
    layout = await git_manager.add_detached_worktree_at_commit(
        workspace_id=snap_id,
        repo_url=str(repo),
        commit_sha=base_sha,
    )
    marker = layout.worktree_path / ".awf" / "workspace.yml"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").startswith("name: adopted-from-marker")
    assert not (layout.worktree_path / "package.json").exists()
    resolution = resolve_workspace_profile(
        worktree_path=layout.worktree_path,
        inline_profile=None,
        profile_ref="auto",
        repo_url=str(repo),
    )
    assert resolution.profile.name == "adopted-from-marker"
    assert resolution.profile.source.startswith("repo:")

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
