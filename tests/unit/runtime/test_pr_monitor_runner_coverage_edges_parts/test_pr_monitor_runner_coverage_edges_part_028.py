"""Additional PR monitor verdict-agent edge coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorMirrorHooksPathRepairFailedError,
)


class _VerdictRunner:
    def __init__(self, tmp_path: Path, adapter: object) -> None:
        self._worktrees_root = tmp_path / "worktrees"
        self._deps = SimpleNamespace(adapter=adapter)

    async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
        return False


@pytest.mark.unit
async def test_run_agent_for_verdict_fails_when_runtime_ownership_repair_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_id = "ws_verdict_ownership"
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError):
        await comments._invoke_cli_for_verdict_result(
            _VerdictRunner(tmp_path, adapter=object()),
            workspace_id=workspace_id,
            prompt="Fix review comment",
            commit_message="fix: review comment",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )


@pytest.mark.unit
async def test_run_agent_for_verdict_repairs_mirror_after_unexpected_agent_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_id = "ws_verdict_mirror"
    mirror = tmp_path / "mirror.git"
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)

    class _Adapter:
        async def run(self, **_kwargs: object) -> object:
            raise RuntimeError("adapter transport failed")

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    async def _repair_mirror_hooks_path(path: Path) -> bool:
        assert path == mirror
        raise OSError("config locked")

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)
    monkeypatch.setattr(comments, "mirror_path_for_worktree", lambda _worktree_path: mirror)
    monkeypatch.setattr(comments, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await comments._invoke_cli_for_verdict_result(
            _VerdictRunner(tmp_path, adapter=_Adapter()),
            workspace_id=workspace_id,
            prompt="Fix review comment",
            commit_message="fix: review comment",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
