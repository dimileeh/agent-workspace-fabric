from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _mock_verify_head_object_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
