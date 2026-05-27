"""Focused coverage for executor configuration validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.executor.config import ExecutorConfig


@pytest.mark.unit
def test_executor_config_rejects_relative_worktrees_root() -> None:
    with pytest.raises(ValueError, match="worktrees_root"):
        ExecutorConfig(
            worktrees_root=Path("relative/worktrees"),
            compose_projects_root=Path("/tmp/compose"),
        )


@pytest.mark.unit
def test_executor_config_rejects_relative_compose_projects_root() -> None:
    with pytest.raises(ValueError, match="compose_projects_root"):
        ExecutorConfig(
            worktrees_root=Path("/tmp/worktrees"),
            compose_projects_root=Path("relative/compose"),
        )
