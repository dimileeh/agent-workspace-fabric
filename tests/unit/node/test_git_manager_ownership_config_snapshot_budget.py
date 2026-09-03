"""Git config snapshot aggregate budget tests for residue fingerprinting."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import awf.node.git_manager_ownership as git_manager_ownership


@pytest.mark.unit
def test_git_config_snapshot_aggregate_byte_budget_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e7pGD: shared byte budget must fail closed across git-dirs."""
    repos: list[Path] = []
    for i in range(3):
        git_dir = tmp_path / f"repo{i}"
        git_dir.mkdir()
        (git_dir / "config").write_bytes(b"x" * 100)
        repos.append(git_dir)

    monkeypatch.setattr(
        git_manager_ownership,
        "_GIT_CONFIG_SNAPSHOT_AGGREGATE_MAX_BYTES",
        250,
    )
    with git_manager_ownership._residue_git_config_snapshot_budget():
        assert git_manager_ownership._snapshot_git_dir_local_configs(repos[0]) == {
            "config": "x" * 100
        }
        assert git_manager_ownership._snapshot_git_dir_local_configs(repos[1]) == {
            "config": "x" * 100
        }
        assert git_manager_ownership._snapshot_git_dir_local_configs(repos[2]) is None


@pytest.mark.unit
def test_git_config_snapshot_aggregate_deadline_fails_closed(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e7pGD: shared wall-time budget must fail closed on config reads."""
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n")

    budget = git_manager_ownership._GitConfigSnapshotBudget(
        bytes_remaining=git_manager_ownership._GIT_CONFIG_SNAPSHOT_AGGREGATE_MAX_BYTES,
        deadline=time.monotonic() - 1.0,
    )
    token = git_manager_ownership._GIT_CONFIG_SNAPSHOT_BUDGET.set(budget)
    try:
        assert git_manager_ownership._snapshot_git_dir_local_configs(git_dir) is None
    finally:
        git_manager_ownership._GIT_CONFIG_SNAPSHOT_BUDGET.reset(token)


@pytest.mark.unit
def test_git_config_snapshot_budget_via_fd_fails_closed_on_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    (git_dir / "config").write_bytes(b"y" * 64)
    fd = git_manager_ownership._open_git_dir_directory_fd(git_dir)
    assert fd is not None
    monkeypatch.setattr(
        git_manager_ownership,
        "_GIT_CONFIG_SNAPSHOT_AGGREGATE_MAX_BYTES",
        32,
    )
    try:
        with git_manager_ownership._residue_git_config_snapshot_budget():
            assert git_manager_ownership._snapshot_git_dir_local_configs_via_fd(fd) is None
    finally:
        os.close(fd)
