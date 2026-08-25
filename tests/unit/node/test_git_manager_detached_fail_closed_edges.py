"""Fail-closed edge coverage for trusted-base detached worktree helpers.

These paths are the intentional defensive branches in
``git_manager_detached`` that happy-path / symlink-leaf tests do not reach:
empty paths, non-blob leaf modes, gitlink parents, malformed Git objects, and
best-effort rmtree after a failed verify cleanup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.node import git_manager_detached as detached_mod
from awf.node.git_manager import GitManager, GitOperationError
from tests.unit.node.test_provisioner_parts._adoption_trusted_base_helpers import (
    _build_stale_head_safe_base_origin,
    git_manager,
)

pytestmark = pytest.mark.unit

__all__ = ["git_manager"]

_COMMIT_SHA = "a" * 40
_TREE_OID = "b" * 40
_BLOB_OID = "c" * 40
_VALID_COMMIT = f"tree {_TREE_OID}\nauthor T <t@t> 1 +0000\n\nmsg\n".encode()


def test_tree_entry_by_name_rejects_malformed_payloads() -> None:
    """Truncated tree entries must return None rather than raise or mis-parse."""
    assert detached_mod._tree_entry_by_name(b"no-space-anywhere", "x") is None
    assert detached_mod._tree_entry_by_name(b"100644 filename-without-nul", "filename") is None
    # Space + name + NUL present, but OID shorter than 20 bytes.
    truncated = b"100644 name\0\x01\x02\x03"
    assert detached_mod._tree_entry_by_name(truncated, "name") is None

    oid = bytes(range(20))
    well_formed = b"100644 name\0" + oid
    assert detached_mod._tree_entry_by_name(well_formed, "name") == ("100644", oid.hex())
    assert detached_mod._tree_entry_by_name(well_formed, "other") is None


def test_tree_oid_from_commit_payload_rejects_invalid_or_missing_tree() -> None:
    """Commits without a 40-hex tree OID must fail closed."""
    with pytest.raises(GitOperationError) as missing:
        detached_mod._tree_oid_from_commit_payload(b"parent " + b"d" * 40 + b"\n")
    assert missing.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"

    with pytest.raises(GitOperationError) as invalid:
        detached_mod._tree_oid_from_commit_payload(b"tree not-a-valid-oid\n")
    assert invalid.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "tree OID" in invalid.value.stderr


@pytest.mark.asyncio
async def test_raw_commit_blob_bytes_empty_relative_path_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty path after split must not walk the tree."""

    async def _fake_read(
        *, mirror_path: Path, env: dict[str, str], oid: str, expected_type: str
    ) -> bytes:
        del mirror_path, env, oid
        assert expected_type == "commit"
        return _VALID_COMMIT

    monkeypatch.setattr(detached_mod, "_read_verified_git_object", _fake_read)
    result = await detached_mod._raw_commit_blob_bytes(
        MagicMock(),
        mirror_path=Path("/tmp/mirror"),
        commit_sha=_COMMIT_SHA,
        relative_path="",
        env={},
    )
    assert result is None
    result_slash = await detached_mod._raw_commit_blob_bytes(
        MagicMock(),
        mirror_path=Path("/tmp/mirror"),
        commit_sha=_COMMIT_SHA,
        relative_path="/",
        env={},
    )
    assert result_slash is None


@pytest.mark.asyncio
async def test_raw_commit_blob_bytes_non_blob_leaf_mode_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaf modes that are neither blob nor symlink/gitlink soft-miss as absent."""

    async def _fake_read(
        *, mirror_path: Path, env: dict[str, str], oid: str, expected_type: str
    ) -> bytes:
        del mirror_path, env, oid
        if expected_type == "commit":
            return _VALID_COMMIT
        return b"tree-bytes"

    monkeypatch.setattr(detached_mod, "_read_verified_git_object", _fake_read)
    monkeypatch.setattr(
        detached_mod,
        "_tree_entry_by_name",
        lambda _payload, _name: ("040000", _BLOB_OID),
    )
    result = await detached_mod._raw_commit_blob_bytes(
        MagicMock(),
        mirror_path=Path("/tmp/mirror"),
        commit_sha=_COMMIT_SHA,
        relative_path=".awf/workspace.yml",
        env={},
    )
    assert result is None


@pytest.mark.asyncio
async def test_raw_commit_blob_bytes_rejects_gitlink_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gitlink profile markers must fail closed under ``--no-checkout``."""

    async def _fake_read(
        *, mirror_path: Path, env: dict[str, str], oid: str, expected_type: str
    ) -> bytes:
        del mirror_path, env, oid
        if expected_type == "commit":
            return _VALID_COMMIT
        return b"tree-bytes"

    monkeypatch.setattr(detached_mod, "_read_verified_git_object", _fake_read)
    monkeypatch.setattr(
        detached_mod,
        "_tree_entry_by_name",
        lambda _payload, name: ("160000", _BLOB_OID) if name == "workspace.yml" else None,
    )
    with pytest.raises(GitOperationError) as raised:
        await detached_mod._raw_commit_blob_bytes(
            MagicMock(),
            mirror_path=Path("/tmp/mirror"),
            commit_sha=_COMMIT_SHA,
            relative_path="workspace.yml",
            env={},
            reject_special_leaf=True,
        )
    assert raised.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "is a gitlink in the commit tree" in raised.value.stderr
    assert "parent component" not in raised.value.stderr


@pytest.mark.asyncio
async def test_raw_commit_blob_bytes_rejects_gitlink_parent_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gitlink parent component must fail closed, not look absent."""

    async def _fake_read(
        *, mirror_path: Path, env: dict[str, str], oid: str, expected_type: str
    ) -> bytes:
        del mirror_path, env, oid
        if expected_type == "commit":
            return _VALID_COMMIT
        return b"tree-bytes"

    monkeypatch.setattr(detached_mod, "_read_verified_git_object", _fake_read)
    monkeypatch.setattr(
        detached_mod,
        "_tree_entry_by_name",
        lambda _payload, name: ("160000", _BLOB_OID) if name == ".awf" else ("100644", _BLOB_OID),
    )
    with pytest.raises(GitOperationError) as raised:
        await detached_mod._raw_commit_blob_bytes(
            MagicMock(),
            mirror_path=Path("/tmp/mirror"),
            commit_sha=_COMMIT_SHA,
            relative_path=".awf/workspace.yml",
            env={},
            reject_special_leaf=True,
        )
    assert raised.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "gitlink" in raised.value.stderr
    assert ".awf" in raised.value.stderr


@pytest.mark.asyncio
async def test_raw_commit_blob_bytes_rejects_non_tree_parent_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Weird non-tree parent modes fail closed with an explicit non-tree kind."""

    async def _fake_read(
        *, mirror_path: Path, env: dict[str, str], oid: str, expected_type: str
    ) -> bytes:
        del mirror_path, env, oid
        if expected_type == "commit":
            return _VALID_COMMIT
        return b"tree-bytes"

    monkeypatch.setattr(detached_mod, "_read_verified_git_object", _fake_read)
    monkeypatch.setattr(
        detached_mod,
        "_tree_entry_by_name",
        lambda _payload, name: ("100644", _BLOB_OID) if name == ".awf" else None,
    )
    with pytest.raises(GitOperationError) as raised:
        await detached_mod._raw_commit_blob_bytes(
            MagicMock(),
            mirror_path=Path("/tmp/mirror"),
            commit_sha=_COMMIT_SHA,
            relative_path=".awf/workspace.yml",
            env={},
            reject_special_leaf=True,
        )
    assert raised.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "non-tree" in raised.value.stderr


@pytest.mark.asyncio
async def test_raw_commit_blob_bytes_soft_skips_special_parent_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autodetect probes must soft-miss special parents instead of aborting."""

    async def _fake_read(
        *, mirror_path: Path, env: dict[str, str], oid: str, expected_type: str
    ) -> bytes:
        del mirror_path, env, oid
        if expected_type == "commit":
            return _VALID_COMMIT
        return b"tree-bytes"

    monkeypatch.setattr(detached_mod, "_read_verified_git_object", _fake_read)
    monkeypatch.setattr(
        detached_mod,
        "_tree_entry_by_name",
        lambda _payload, _name: ("160000", _BLOB_OID),
    )
    result = await detached_mod._raw_commit_blob_bytes(
        MagicMock(),
        mirror_path=Path("/tmp/mirror"),
        commit_sha=_COMMIT_SHA,
        relative_path="vendor/package.json",
        env={},
        reject_special_leaf=False,
    )
    assert result is None


@pytest.mark.asyncio
async def test_read_verified_git_object_fails_closed_on_cat_file_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero ``git cat-file`` must surface as a trusted-base mismatch."""

    class _FakeProc:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stdout-noise", b"fatal: not a valid object"

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    with pytest.raises(GitOperationError) as raised:
        await detached_mod._read_verified_git_object(
            mirror_path=tmp_path,
            env={},
            oid=_COMMIT_SHA,
            expected_type="commit",
        )
    assert raised.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert raised.value.returncode == 128
    assert "not a valid object" in raised.value.stderr
    assert raised.value.stdout == "stdout-noise"


@pytest.mark.asyncio
async def test_verify_failure_rmtrees_when_worktree_remove_fails(
    git_manager: GitManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``worktree remove`` fails after verify, rmtree must still clear the path."""
    repo, base_sha, _ = _build_stale_head_safe_base_origin(tmp_path / "git")
    snap_id = "ws_snap_rmtree_fallback__trusted_base_profile"
    worktree_path = git_manager._worktree_path_for(snap_id)
    real_run = git_manager._run

    async def _boom_verify(*_args: Any, **_kwargs: Any) -> None:
        assert worktree_path.exists()
        raise GitOperationError(
            operation="mirror.verify_trusted_base_profile",
            returncode=1,
            stdout="",
            stderr="forced verify failure",
            reason_code="GIT_TRUSTED_BASE_PROFILE_MISMATCH",
        )

    async def _run(args: list[str], *, operation: str, env: Any = None) -> Any:
        if operation == "worktree.remove_trusted_base_verify_failed":
            # Simulate a remove that cannot clear the directory.
            raise GitOperationError(
                operation=operation,
                returncode=1,
                stdout="",
                stderr="remove failed",
                reason_code="GIT_COMMAND_FAILED",
            )
        return await real_run(args, operation=operation, env=env)

    monkeypatch.setattr(git_manager, "_run", _run)
    monkeypatch.setattr(
        detached_mod,
        "_verify_and_materialize_trusted_profile_markers",
        _boom_verify,
    )

    with pytest.raises(GitOperationError) as raised:
        await git_manager.add_detached_worktree_at_commit(
            workspace_id=snap_id, repo_url=str(repo), commit_sha=base_sha
        )
    assert raised.value.reason_code == "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
    assert "forced verify failure" in raised.value.stderr
    assert not worktree_path.exists()
