"""Diagnostic disposal removes owned resources only after runtime quiescence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.control.worker.cleanup import _release_terminal_runtime_for_candidate
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.cleanup import WorkspaceCleaner, WorkspaceCleanupResult
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitOperationError


@pytest.mark.parametrize("ok", [False, True])
async def test_worker_diagnostics_are_disposable_and_results_outlive_fixture(monkeypatch, ok):
    from awf.node.cleanup import WorkspaceCleanupStepResult

    result = (
        WorkspaceCleanupResult.skipped()
        if ok
        else WorkspaceCleanupResult.from_steps(
            [WorkspaceCleanupStepResult("compose_down", "failed", "FAILED")]
        )
    )
    worker = SimpleNamespace(
        _runtime_cleaner=SimpleNamespace(cleanup=AsyncMock(return_value=result)),
        _record_terminal_runtime_released=AsyncMock(),
        _record_terminal_runtime_release_failed=AsyncMock(),
    )
    monkeypatch.setattr(
        "awf.control.worker.cleanup._teardown_terminal_auth_overlay", AsyncMock(return_value=None)
    )
    candidate = _TerminalRuntimeCandidate(
        "ws_diag",
        WorkspaceStatus.failed,
        "awf_ws_diag",
        None,
        "awf-internal://provider-diagnostic/v1",
        task_policy={"provider_diagnostic": {"result": {"outcome": "inconclusive"}}},
    )
    await _release_terminal_runtime_for_candidate(worker, candidate)
    assert worker._runtime_cleaner.cleanup.call_args.kwargs["remove_volumes"] is True
    assert worker._runtime_cleaner.cleanup.call_args.kwargs["remove_worktree"] is True
    assert candidate.task_policy["provider_diagnostic"]["result"]["outcome"] == "inconclusive"
    assert worker._record_terminal_runtime_released.await_count == int(ok)
    assert worker._record_terminal_runtime_release_failed.await_count == int(not ok)


async def test_failed_teardown_retains_diagnostic_fixture(tmp_path):
    error = ComposeOperationError(
        operation="down", returncode=1, stdout="", stderr="cannot stop", reason_code="DOWN_FAILED"
    )
    compose = SimpleNamespace(
        down=AsyncMock(side_effect=error), remove_project_by_label=AsyncMock(side_effect=error)
    )
    git = SimpleNamespace(remove_worktree=AsyncMock(), _worktrees_dir=tmp_path)
    result = await WorkspaceCleaner(git=git, compose=compose).cleanup(
        workspace_id="ws_diag",
        repo_url="awf-internal://provider-diagnostic/v1",
        remove_worktree=True,
    )
    assert not result.ok
    git.remove_worktree.assert_not_awaited()


@pytest.mark.parametrize(
    ("agent", "relative"),
    [("codex", ".codex/auth.json"), ("claude_code", ".claude/.credentials.json")],
)
@pytest.mark.parametrize("outcome", ["succeeded", "compose_failed", "worktree_failed", "retained"])
async def test_service_cleaner_uses_resolver_auth_root(
    tmp_path, monkeypatch, agent, relative, outcome
):
    from pathlib import Path

    from awf.common.config import Settings
    from awf.service.controls_helpers import default_cleaner

    home = tmp_path / "host"
    source = home / relative
    source.parent.mkdir(parents=True)
    source.write_text("synthetic-native-credential")
    resolver = ServiceAuthMountResolver(host_home=home, work_dir=tmp_path / "state")
    mount = resolver.resolve_diagnostic_auth(workspace_id="ws_diag", agent=agent)[0]
    other_mount = resolver.resolve_diagnostic_auth(workspace_id="ws_other", agent=agent)[0]
    unrelated = resolver.work_dir / "auth" / "ws_diag" / "keep.json"
    unrelated.write_text("unrelated-auth")
    cleaner = default_cleaner(settings=Settings(_env_file=None, work_dir=str(resolver.work_dir)))
    assert cleaner._git.work_dir == resolver.work_dir / "git"
    down = AsyncMock(return_value=True)
    reap = AsyncMock()
    remove = AsyncMock()
    if outcome == "compose_failed":
        error = ComposeOperationError(operation="down", returncode=1, stdout="", stderr="busy")
        down.side_effect = error
        reap.side_effect = error
    if outcome == "worktree_failed":
        remove.side_effect = GitOperationError(
            operation="remove", returncode=1, stdout="", stderr="protected"
        )
    monkeypatch.setattr(cleaner._compose, "down", down)
    monkeypatch.setattr(cleaner._compose, "remove_project_by_label", reap)
    monkeypatch.setattr(cleaner._git, "remove_worktree", remove)

    result = await cleaner.cleanup(
        workspace_id="ws_diag",
        repo_url="awf-internal://provider-diagnostic/v1",
        remove_worktree=outcome != "retained",
    )

    assert result.ok is (outcome in {"succeeded", "retained"})
    assert Path(mount.source).exists() is (outcome != "succeeded")
    assert Path(other_mount.source).is_dir()
    assert source.read_text() == "synthetic-native-credential"
    assert unrelated.read_text() == "unrelated-auth"


async def test_diagnostic_auth_removal_failure_is_reported(tmp_path, monkeypatch):
    from awf.common.config import Settings
    from awf.service.controls_helpers import default_cleaner

    cleaner = default_cleaner(settings=Settings(_env_file=None, work_dir=str(tmp_path)))
    monkeypatch.setattr(cleaner._compose, "down", AsyncMock(return_value=True))
    monkeypatch.setattr(cleaner._compose, "remove_project_by_label", AsyncMock())
    monkeypatch.setattr(cleaner._git, "remove_worktree", AsyncMock())
    root = tmp_path / "auth" / "ws_diag"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credential").write_text("synthetic-native-credential")
    (root / "diagnostic").symlink_to(outside, target_is_directory=True)

    result = await cleaner.cleanup(
        workspace_id="ws_diag", repo_url="awf-internal://provider-diagnostic/v1"
    )

    assert not result.ok
    assert result.failed_steps[0].name == "diagnostic_auth_remove"
    assert result.failed_steps[0].reason_code == "PROVIDER_DIAGNOSTIC_CLEANUP_FAILED"
    assert (outside / "credential").read_text() == "synthetic-native-credential"
