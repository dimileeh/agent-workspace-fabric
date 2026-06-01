"""Focused coverage for PR monitor path helper modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.quality_gates import QualityGateViolation
from awf.runtime import git_porcelain, validation_worktree
from awf.runtime.pr_monitor_runner import path_parsing
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _worktree_modified_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.path_helpers import (
    _changed_paths_from_name_only_z,
    _quality_gate_violation_paths,
    _read_worktree_text,
    _supply_chain_policy_blocked_message,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

pytestmark = pytest.mark.unit


def test_name_only_z_parser_rejects_non_nul_and_truncated_output() -> None:
    assert _changed_paths_from_name_only_z("") == ()
    assert _changed_paths_from_name_only_z("src/a.py\0src/b.py\0src/a.py\0") == (
        "src/a.py",
        "src/b.py",
    )

    with pytest.raises(ProtectedScopeDiffError, match="expected NUL-delimited"):
        _changed_paths_from_name_only_z("src/a.py\nsrc/b.py\n")
    with pytest.raises(ProtectedScopeDiffError, match="missing terminating NUL"):
        _changed_paths_from_name_only_z("src/a.py\0src/b.py")
    with pytest.raises(ProtectedScopeDiffError, match="empty path"):
        _changed_paths_from_name_only_z("src/a.py\0\0")


def test_policy_path_helpers_deduplicate_and_format_messages() -> None:
    violations = [
        QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        QualityGateViolation(path=".github/workflows/ci.yml", protected_pattern=".github/**"),
    ]

    assert _quality_gate_violation_paths(violations) == [
        "pyproject.toml",
        ".github/workflows/ci.yml",
    ]
    assert _supply_chain_policy_blocked_message([]) == (
        "Supply-chain policy blocked PR monitor publication."
    )
    assert _supply_chain_policy_blocked_message(["LOCKFILE_CHANGED", "LOCKFILE_CHANGED"]) == (
        "Supply-chain policy blocked PR monitor publication: LOCKFILE_CHANGED"
    )


def test_read_worktree_text_success_and_failures(tmp_path: Path) -> None:
    readable = tmp_path / "pyproject.toml"
    readable.write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    assert _read_worktree_text(readable) == "[project]\nname = 'awf'\n"

    binary = tmp_path / "binary.lock"
    binary.write_bytes(b"\xff")
    with pytest.raises(ProtectedScopeDiffError, match="as UTF-8"):
        _read_worktree_text(binary, display_path="binary.lock")

    with pytest.raises(ProtectedScopeDiffError, match="missing.toml"):
        _read_worktree_text(tmp_path / "missing.toml")


def test_path_parsing_name_only_z_helper_remains_deduplicating() -> None:
    assert path_parsing._changed_paths_from_name_only_z("") == ()  # noqa: SLF001
    assert path_parsing._changed_paths_from_name_only_z("src/one.py") == (  # noqa: SLF001
        "src/one.py",
    )
    assert path_parsing._changed_paths_from_name_only_z(  # noqa: SLF001
        "src/a.py\0src/b.py\0src/a.py\0"
    ) == ("src/a.py", "src/b.py")
    with pytest.raises(ProtectedScopeDiffError, match="empty path"):
        path_parsing._changed_paths_from_name_only_z("src/a.py\0\0")  # noqa: SLF001


def test_path_parsing_porcelain_z_records_handles_unterminated_output() -> None:
    assert path_parsing._porcelain_z_records(" M src/one.py") == [  # noqa: SLF001
        (" M", "src/one.py", None)
    ]


def test_non_nul_porcelain_parser_uses_shared_runtime_helpers() -> None:
    status_stdout = 'R  "old\\040name.py" -> "new\\040name.py"\n!! "ignored\\040root/"\n'

    assert (  # noqa: SLF001
        validation_worktree._changed_paths_from_porcelain
        is git_porcelain.changed_paths_from_porcelain
    )
    assert (  # noqa: SLF001
        validation_worktree._untracked_paths_from_porcelain
        is git_porcelain.untracked_paths_from_porcelain
    )
    assert (  # noqa: SLF001
        validation_worktree._unquote_porcelain_path is git_porcelain.unquote_porcelain_path
    )
    assert (  # noqa: SLF001
        path_parsing._split_porcelain_rename_paths is git_porcelain.split_porcelain_rename_paths
    )
    assert (  # noqa: SLF001
        path_parsing._unquote_porcelain_path is git_porcelain.unquote_porcelain_path
    )
    assert path_parsing._changed_paths_from_porcelain(status_stdout) == [  # noqa: SLF001
        "old name.py",
        "new name.py",
        "ignored root/",
    ]
    assert path_parsing._untracked_paths_from_porcelain(status_stdout) == [  # noqa: SLF001
        "ignored root/",
    ]


def test_worktree_modified_paths_from_porcelain_skips_malformed_and_staged() -> None:
    assert _worktree_modified_paths_from_porcelain(  # noqa: SLF001
        "\n"
        "M\n"
        "M  staged-only.py\n"
        "?? untracked.py\n"
        " M worktree.py\n"
        'RM "old\\040name.py" -> "new\\040name.py"\n'
        " M worktree.py\n"
    ) == ["worktree.py", "new name.py"]
