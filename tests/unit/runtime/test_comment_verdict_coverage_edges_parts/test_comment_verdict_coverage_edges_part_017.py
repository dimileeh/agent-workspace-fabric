"""Top-level ignored-file overflow identity (part 17)."""

from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path

import pytest

from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
)


@pytest.mark.unit
def test_ignored_file_hash_falls_back_to_metadata_when_content_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized top-level ``!!`` files must not yield None identity.

    Directory entries already overflow to metadata when the 8 MiB / 32 MiB
    regular-hash budgets fail closed. Top-level ignored files never entered that
    fallback, so a clean non-FIXED correction against a large ``!! blob`` was
    rejected as mutation (review 5107935328).
    """
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_file_budget"
    worktree.mkdir()
    init_git_worktree(worktree)
    blob = worktree / "large.bin"
    blob.write_bytes(b"baseline-payload\n")

    monkeypatch.setattr(
        residue,
        "_hash_untracked_residue_paths",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["large.bin"],
        git_env=git_env,
    )
    assert baseline is not None
    repeat = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["large.bin"],
        git_env=git_env,
    )
    assert repeat == baseline
    blob.write_bytes(b"mutated-payload-longer\n")
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["large.bin"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != baseline


@pytest.mark.unit
@pytest.mark.timeout(60)
def test_ignored_file_metadata_fallback_stable_with_oversized_regular_file(
    tmp_path: Path,
) -> None:
    """A real >8 MiB top-level ignored file must fingerprint without None."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_file_oversized"
    worktree.mkdir()
    init_git_worktree(worktree)
    sample = io_mod._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    oversize = io_mod._WORKTREE_REGULAR_HASH_MAX_FILE_BYTES + sample
    blob = worktree / "cache.bin"
    blob.write_bytes(b"L" * oversize)

    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["cache.bin"],
        git_env=git_env,
    )
    assert baseline is not None
    repeat = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["cache.bin"],
        git_env=git_env,
    )
    assert repeat == baseline


@pytest.mark.unit
def test_ignored_file_metadata_fallback_hashes_symlink_when_content_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regular ignored leaves still fingerprint on the overflow path."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_file_symlink"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "target.txt").write_text("target\n", encoding="utf-8")
    (worktree / "link.bin").symlink_to("target.txt")

    monkeypatch.setattr(
        residue,
        "_hash_untracked_residue_paths",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["link.bin"],
        git_env=git_env,
    )
    assert baseline is not None
    (worktree / "link.bin").unlink()
    (worktree / "link.bin").symlink_to("missing-target")
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["link.bin"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != baseline


@pytest.mark.unit
def test_ignored_file_metadata_fallback_fails_closed_when_kind_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow identity fails closed when a leaf cannot be classified."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_file_kind"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "gone.bin").write_bytes(b"x\n")
    monkeypatch.setattr(residue, "_hash_untracked_residue_paths", lambda **_kwargs: None)
    monkeypatch.setattr(io_mod, "_worktree_entry_kind", lambda _candidate: None)
    assert (
        fp_mod._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["gone.bin"],
            git_env=git_env_without_object_lookup_overrides(),
        )
        is None
    )


@pytest.mark.unit
def test_ignored_file_metadata_fallback_fails_closed_on_open_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow identity fails closed when the regular-file open raises."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_file_open"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "gone.bin").write_bytes(b"x\n")
    monkeypatch.setattr(residue, "_hash_untracked_residue_paths", lambda **_kwargs: None)

    @contextlib.contextmanager
    def _boom_open(*_args: object, **_kwargs: object) -> object:
        raise OSError(errno.EACCES, "denied")
        yield  # pragma: no cover

    monkeypatch.setattr(io_mod, "_open_worktree_regular_file_under_root", _boom_open)
    assert (
        fp_mod._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["gone.bin"],
            git_env=git_env_without_object_lookup_overrides(),
        )
        is None
    )


@pytest.mark.unit
def test_ignored_file_metadata_fallback_fails_closed_on_fstat_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow identity fails closed when fstat on the opened leaf raises."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_file_fstat"
    worktree.mkdir()
    init_git_worktree(worktree)
    blob = worktree / "gone.bin"
    blob.write_bytes(b"x\n")
    monkeypatch.setattr(residue, "_hash_untracked_residue_paths", lambda **_kwargs: None)

    @contextlib.contextmanager
    def _opened_then_fstat_fails(*_args: object, **_kwargs: object) -> object:
        with blob.open("rb") as fh:

            def _bad_fileno() -> int:
                raise OSError(errno.EBADF, "bad fd")

            fh.fileno = _bad_fileno  # type: ignore[method-assign]
            yield fh

    monkeypatch.setattr(
        io_mod,
        "_open_worktree_regular_file_under_root",
        _opened_then_fstat_fails,
    )
    assert (
        fp_mod._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["gone.bin"],
            git_env=git_env_without_object_lookup_overrides(),
        )
        is None
    )


@pytest.mark.unit
def test_ignored_file_metadata_fallback_fails_closed_when_samples_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow identity fails closed when content sampling returns False."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_file_samples"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "gone.bin").write_bytes(b"x\n")
    monkeypatch.setattr(residue, "_hash_untracked_residue_paths", lambda **_kwargs: None)
    monkeypatch.setattr(
        io_mod,
        "_hash_regular_file_content_samples_into",
        lambda *_args, **_kwargs: False,
    )
    assert (
        fp_mod._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["gone.bin"],
            git_env=git_env_without_object_lookup_overrides(),
        )
        is None
    )


@pytest.mark.unit
def test_ignored_file_metadata_fallback_fails_closed_when_other_digest_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow identity fails closed when non-regular digest returns None."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_file_other"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "target.txt").write_text("t\n", encoding="utf-8")
    (worktree / "link.bin").symlink_to("target.txt")
    monkeypatch.setattr(residue, "_hash_untracked_residue_paths", lambda **_kwargs: None)
    monkeypatch.setattr(residue, "_digest_worktree_entry_bytes", lambda **_kwargs: None)
    assert (
        fp_mod._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["link.bin"],
            git_env=git_env_without_object_lookup_overrides(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.timeout(60)
def test_ignored_file_metadata_fallback_detects_oversized_middle_only_overwrite(
    tmp_path: Path,
) -> None:
    """Overflow identity for top-level ``!!`` files must see middle-only edits."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_io as io_mod

    worktree = tmp_path / "ws_ignored_file_middle"
    worktree.mkdir()
    init_git_worktree(worktree)
    sample = io_mod._WORKTREE_REGULAR_HASH_CHUNK_BYTES
    oversize = io_mod._WORKTREE_REGULAR_HASH_MAX_FILE_BYTES + sample
    mid = oversize - 2 * sample
    target = worktree / "artifact.bin"
    target.write_bytes(b"H" * sample + b"M" * mid + b"T" * sample)

    git_env = git_env_without_object_lookup_overrides()
    start = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["artifact.bin"],
        git_env=git_env,
    )
    assert start is not None

    st = target.stat()
    target.write_bytes(b"H" * sample + b"X" * mid + b"T" * sample)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["artifact.bin"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != start
