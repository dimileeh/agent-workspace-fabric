"""Additional PR monitor remote-repair edge coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorHeadObjectMissingError,
    _MonitorPolicyBlockedError,
)

_WORKSPACE_ID = "ws_remote_repair_edges"
_START_HEAD = "1" * 40
_BRANCH_REF = f"refs/heads/awf/{_WORKSPACE_ID}"


class _RecoveryRunner:
    def __init__(self, cmd: FakeCommandRunner, *, policy_message: str | None = None) -> None:
        self._deps = SimpleNamespace(runner=cmd)
        self.policy_message = policy_message
        self.policy_calls: list[tuple[str, ...]] = []

    async def _refresh_supply_chain_policy_before_push(
        self,
        *,
        workspace_id: str,
        command_evidence: object,
        changed_paths: list[str],
    ) -> str | None:
        del command_evidence
        assert workspace_id == _WORKSPACE_ID
        self.policy_calls.append(tuple(changed_paths))
        return self.policy_message


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


async def _recover(
    runner: _RecoveryRunner,
    worktree: Path,
) -> str | None:
    return await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=_WORKSPACE_ID,
        worktree_path=worktree,
        operation_start_head=_START_HEAD,
        expected_branch_ref=_BRANCH_REF,
        command_evidence=("pytest -q",),
    )


def _patch_recovery_basics(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mirror: Path,
) -> None:
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _resolve_branch(_worktree_path: Path) -> str:
        return _BRANCH_REF

    monkeypatch.setattr(pr_remote_repair, "_resolve_worktree_branch_ref", _resolve_branch)
    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_writable_worktree",
        lambda _mirror_path, _worktree_path: None,
    )


def _queue_results(
    cmd: FakeCommandRunner,
    results: tuple[tuple[int, str, str], ...],
) -> None:
    for returncode, stdout, stderr in results:
        cmd.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
async def test_recovered_dirty_protected_scope_fails_closed_when_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, workspace_id: str) -> None:
            assert workspace_id == _WORKSPACE_ID

    runner = SimpleNamespace(_deps=SimpleNamespace(session_factory=lambda: _SessionContext()))
    monkeypatch.setattr(pr_remote_repair, "WorkspaceRepository", _Repository)

    with pytest.raises(ProtectedScopeDiffError, match="Workspace row .* disappeared"):
        await pr_remote_repair._protected_scope_violations_for_recovered_dirty_commit(
            runner,
            workspace_id=_WORKSPACE_ID,
            worktree_path=tmp_path,
            base_ref="origin/main",
            changed_paths=("src/app.py",),
        )


@pytest.mark.unit
async def test_recovered_dirty_protected_scope_wraps_diff_read_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _workspace_id: str) -> object:
            return SimpleNamespace(owned_paths=["src/**"])

    async def _read_diffs(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("git show failed")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=FakeCommandRunner(),
            session_factory=lambda: _SessionContext(),
        )
    )
    monkeypatch.setattr(pr_remote_repair, "WorkspaceRepository", _Repository)
    monkeypatch.setattr(pr_remote_repair, "protected_file_diffs_for_committed_paths", _read_diffs)

    with pytest.raises(ProtectedScopeDiffError, match="git show failed"):
        await pr_remote_repair._protected_scope_violations_for_recovered_dirty_commit(
            runner,
            workspace_id=_WORKSPACE_ID,
            worktree_path=tmp_path,
            base_ref="origin/main",
            changed_paths=("src/app.py",),
        )


@pytest.mark.unit
async def test_recovered_dirty_protected_scope_returns_quality_gate_violations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = [QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml")]

    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _workspace_id: str) -> object:
            return SimpleNamespace(owned_paths=["src/**"])

    async def _read_diffs(*_args: object, **_kwargs: object) -> object:
        return {"pyproject.toml": object()}

    def _find_violations(**kwargs: Any) -> list[QualityGateViolation]:
        assert kwargs["changed_paths"] == ("pyproject.toml",)
        assert kwargs["owned_paths"] == ["src/**"]
        return expected

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=FakeCommandRunner(),
            session_factory=lambda: _SessionContext(),
        )
    )
    monkeypatch.setattr(pr_remote_repair, "WorkspaceRepository", _Repository)
    monkeypatch.setattr(pr_remote_repair, "protected_file_diffs_for_committed_paths", _read_diffs)
    monkeypatch.setattr(pr_remote_repair, "find_protected_quality_gate_changes", _find_violations)

    violations = await pr_remote_repair._protected_scope_violations_for_recovered_dirty_commit(
        runner,
        workspace_id=_WORKSPACE_ID,
        worktree_path=tmp_path,
        base_ref="origin/main",
        changed_paths=("pyproject.toml",),
    )

    assert violations == expected


@pytest.mark.unit
async def test_recovery_cleans_up_when_runtime_unstage_followup_diff_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    _queue_results(
        cmd,
        (
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "A\0.claude/agent-memory/reviewer.json\0", ""),
            (0, "", ""),
            (1, "", "diff failed"),
            (0, "", ""),
        ),
    )
    runner = _RecoveryRunner(cmd)
    _patch_recovery_basics(monkeypatch, mirror=tmp_path / "mirror.git")

    assert await _recover(runner, tmp_path / "worktree") is None
    assert any(call.args[-4:] == ["diff", "--cached", "--name-status", "-z"] for call in cmd.calls)
    assert cmd.calls[-1].args[-3:] == ["reset", "--hard", _START_HEAD]


@pytest.mark.unit
async def test_recovery_policy_block_reports_failed_cleanup_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    _queue_results(
        cmd,
        (
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "A\0generated.tmp\0", ""),
            (1, "", "reset failed"),
        ),
    )
    runner = _RecoveryRunner(cmd, policy_message="blocked package manager side effect")
    _patch_recovery_basics(monkeypatch, mirror=tmp_path / "mirror.git")

    with pytest.raises(_MonitorPolicyBlockedError, match="blocked package manager"):
        await _recover(runner, tmp_path / "worktree")

    assert runner.policy_calls == [("generated.tmp",)]
    assert cmd.calls[-1].args[-3:] == ["reset", "--hard", _START_HEAD]


@pytest.mark.unit
async def test_recovery_policy_block_reports_failed_untracked_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    _queue_results(
        cmd,
        (
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "A\0generated.tmp\0", ""),
            (0, "", ""),
            (1, "", "clean failed"),
        ),
    )
    runner = _RecoveryRunner(cmd, policy_message="blocked generated file")
    _patch_recovery_basics(monkeypatch, mirror=tmp_path / "mirror.git")

    with pytest.raises(_MonitorPolicyBlockedError, match="blocked generated file"):
        await _recover(runner, tmp_path / "worktree")

    assert cmd.calls[-1].args[-5:] == [
        "--literal-pathspecs",
        "clean",
        "-fd",
        "--",
        "generated.tmp",
    ]


@pytest.mark.unit
async def test_recovery_commit_failure_cleans_untracked_residue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    _queue_results(
        cmd,
        (
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "A\0generated.tmp\0", ""),
            (1, "", "commit failed"),
            (0, "", ""),
            (1, "", "clean failed"),
        ),
    )
    runner = _RecoveryRunner(cmd)
    _patch_recovery_basics(monkeypatch, mirror=tmp_path / "mirror.git")

    assert await _recover(runner, tmp_path / "worktree") is None
    assert any("commit" in call.args for call in cmd.calls)
    assert cmd.calls[-1].args[-5:] == [
        "--literal-pathspecs",
        "clean",
        "-fd",
        "--",
        "generated.tmp",
    ]


@pytest.mark.unit
async def test_recovery_returns_none_when_recovered_head_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    _queue_results(
        cmd,
        (
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (1, "", "no head"),
            (0, "", ""),
        ),
    )
    runner = _RecoveryRunner(cmd)
    _patch_recovery_basics(monkeypatch, mirror=tmp_path / "mirror.git")

    assert await _recover(runner, tmp_path / "worktree") is None
    assert cmd.calls[-2].args[-2:] == ["rev-parse", "HEAD"]
    assert cmd.calls[-1].args[-3:] == ["reset", "--hard", _START_HEAD]


@pytest.mark.unit
async def test_commit_dirty_worktree_raises_when_head_missing_without_recovery_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=cmd),
        _worktrees_root=tmp_path / "worktrees",
    )
    worktree = runner._worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)
    monkeypatch.setattr(
        pr_remote_repair, "mirror_path_for_worktree", lambda _path: tmp_path / "m.git"
    )

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _head_missing(_worktree_path: Path) -> bool:
        return False

    async def _no_merge_candidate(_self: object, workspace_id: str) -> None:
        assert workspace_id == _WORKSPACE_ID

    monkeypatch.setattr(pr_remote_repair, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    monkeypatch.setattr(pr_remote_repair, "verify_head_object_exists", _head_missing)
    monkeypatch.setattr(pr_remote_repair, "_open_merge_candidate_head_sha", _no_merge_candidate)

    with pytest.raises(_MonitorHeadObjectMissingError) as exc:
        await pr_remote_repair._commit_dirty_worktree(
            runner,
            workspace_id=_WORKSPACE_ID,
            message="fix: repair",
        )

    assert exc.value.reason_code == _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON


@pytest.mark.unit
async def test_commit_dirty_worktree_returns_false_when_stage_filter_leaves_only_runtime_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M src/app.py\n")
    cmd.queue_result(returncode=0, stdout="?? .claude/agent-memory/reviewer.json\n")
    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=cmd),
        _worktrees_root=tmp_path / "worktrees",
        _refresh_supply_chain_policy_before_push=lambda **_kwargs: None,
    )
    worktree = runner._worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: None)

    async def _head_exists(_worktree_path: Path) -> bool:
        return True

    async def _repair_ownership(**_kwargs: object) -> bool:
        return True

    async def _refresh_policy(**_kwargs: object) -> None:
        return None

    runner._refresh_supply_chain_policy_before_push = _refresh_policy
    monkeypatch.setattr(pr_remote_repair, "verify_head_object_exists", _head_exists)
    monkeypatch.setattr(pr_remote_repair, "repair_agent_runtime_ownership", _repair_ownership)

    committed = await pr_remote_repair._commit_dirty_worktree(
        runner,
        workspace_id=_WORKSPACE_ID,
        message="fix: repair",
    )

    assert committed is False
    assert len(cmd.calls) == 2
