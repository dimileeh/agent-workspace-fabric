"""Regression coverage for AWF's local pre-commit contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _yaml(path: str) -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_pre_commit_runs_project_ruff_commands_that_match_ci() -> None:
    config = _yaml(".pre-commit-config.yaml")
    local_repos = [repo for repo in config["repos"] if repo.get("repo") == "local"]
    assert local_repos

    hooks = {
        hook["id"]: hook for repo in local_repos for hook in repo["hooks"] if isinstance(hook, dict)
    }

    assert hooks["awf-ruff-check"]["entry"] == ("uv run --python 3.12 --extra dev ruff check .")
    assert hooks["awf-ruff-check"]["language"] == "system"
    assert hooks["awf-ruff-check"]["pass_filenames"] is False
    assert hooks["awf-ruff-format-check"]["entry"] == (
        "uv run --python 3.12 --extra dev ruff format --check ."
    )
    assert hooks["awf-ruff-format-check"]["language"] == "system"
    assert hooks["awf-ruff-format-check"]["pass_filenames"] is False
    assert hooks["awf-mypy"]["entry"] == "uv run --python 3.12 --extra dev mypy"
    assert hooks["awf-mypy"]["language"] == "system"
    assert hooks["awf-mypy"]["pass_filenames"] is False
    assert hooks["awf-mypy"]["files"] == "^(src|tests)/"


@pytest.mark.unit
def test_awf_ruff_format_check_entry_is_stable_for_executor_classifier() -> None:
    """``WorkspaceExecutor._classify_post_agent_commit_failure`` matches
    pre-commit output by the ``awf-ruff-format-check`` hook id. If a
    future PR renames the hook or splits ``ruff format --check`` into
    multiple entries, the executor's deterministic format-repair path
    will no longer fire — this test fails fast and forces the change
    set to revisit ``_classify_post_agent_commit_failure``.
    """
    config = _yaml(".pre-commit-config.yaml")
    local_repos = [repo for repo in config["repos"] if repo.get("repo") == "local"]
    hooks = {
        hook["id"]: hook for repo in local_repos for hook in repo["hooks"] if isinstance(hook, dict)
    }
    assert "awf-ruff-format-check" in hooks
    assert hooks["awf-ruff-format-check"]["entry"].endswith("ruff format --check .")


@pytest.mark.unit
def test_pre_commit_does_not_use_isolated_mypy_mirror() -> None:
    config = _yaml(".pre-commit-config.yaml")

    assert all(
        repo.get("repo") != "https://github.com/pre-commit/mirrors-mypy" for repo in config["repos"]
    )


@pytest.mark.unit
def test_awf_self_profile_installs_pre_commit_hooks_before_agent_work() -> None:
    profile = _yaml(".awf/workspace.yml")
    setup_commands = [
        command["command"]
        for command in profile["awf"]["phases"]["setup"]
        if isinstance(command, dict)
    ]

    assert "uv run --python 3.12 --extra dev pre-commit install --install-hooks" in setup_commands
    assert setup_commands.index("uv sync --extra dev") < setup_commands.index(
        "uv run --python 3.12 --extra dev pre-commit install --install-hooks"
    )
