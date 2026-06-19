from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _mock_pr_monitor_git_mirror_guards(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Keep PR-monitor integration tests on fake runners unless they opt in."""

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _commit_object_exists(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    for module_name in (
        "awf.runtime.pr_monitor_runner.ci_ops",
        "awf.runtime.pr_monitor_runner.comments",
        "awf.runtime.pr_monitor_runner.pre_push_validation",
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass",
        "awf.runtime.pr_monitor_runner.remote_ops",
        "awf.runtime.pr_monitor_runner.remote_repair",
        "awf.runtime.pr_monitor_runner.remote_repair_protected",
    ):
        monkeypatch.setattr(
            f"{module_name}.verify_head_object_exists",
            _verify_head_object_exists,
            raising=False,
        )
        monkeypatch.setattr(
            f"{module_name}.repair_mirror_hooks_path",
            _repair_mirror_hooks_path,
            raising=False,
        )
        monkeypatch.setattr(
            f"{module_name}.repair_agent_runtime_ownership",
            _repair_agent_runtime_ownership,
            raising=False,
        )

    if "repair_operation_start_head" not in request.node.nodeid:
        monkeypatch.setattr(
            "awf.runtime.pr_monitor_runner.remote_repair._mirror_commit_object_exists",
            _commit_object_exists,
            raising=False,
        )
        monkeypatch.setattr(
            "awf.runtime.pr_monitor_runner.remote_repair._worktree_commit_object_exists",
            _commit_object_exists,
            raising=False,
        )
