"""Focused coverage tests for PR monitor missing-HEAD recovery helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.quality_gates_common import QualityGateViolation
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner import (
    remote_repair_protected as pr_remote_repair_protected,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

_WORKSPACE_ID = "ws_recovery_edges"
_START_HEAD = "1" * 40
_RECOVERED_HEAD = "2" * 40
_BRANCH_REF = "refs/heads/awf/ws_recovery_edges"


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


class _CommitRunner(_RecoveryRunner):
    def __init__(self, cmd: FakeCommandRunner, *, worktrees_root: Path) -> None:
        super().__init__(cmd)
        self._worktrees_root = worktrees_root
        self.protected_repair_calls = 0

    async def _repair_protected_scope_changes_before_commit(
        self, **_kwargs: object
    ) -> CommandResult:
        self.protected_repair_calls += 1
        return CommandResult(returncode=0, stdout="", stderr="")


class _ProtectedRepairRunner:
    def __init__(self, cmd: FakeCommandRunner, *, worktrees_root: Path) -> None:
        self._deps = SimpleNamespace(
            runner=cmd,
            adapter=_UnexpectedFailureAdapter(),
        )
        self._worktrees_root = worktrees_root
        self.violations_calls = 0

    async def _protected_scope_violations_for_status(
        self,
        **_kwargs: object,
    ) -> list[QualityGateViolation]:
        self.violations_calls += 1
        return [
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/workflows/",
            )
        ]

    async def _protected_scope_repair_prompt(self, **_kwargs: object) -> str:
        return "repair protected scope"

    async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
        return False

    async def _rev_parse_head(self, _worktree_path: Path) -> str | None:
        return None


class _UnexpectedFailureAdapter:
    async def run(self, **_kwargs: object) -> None:
        raise RuntimeError("compose cleanup failed")


async def _recover(
    runner: _RecoveryRunner,
    worktree: Path,
    *,
    task_tag: str | None = None,
) -> str | None:
    return await pr_remote_repair._recover_missing_head_object_from_filesystem(
        runner,
        workspace_id=_WORKSPACE_ID,
        worktree_path=worktree,
        operation_start_head=_START_HEAD,
        task_tag=task_tag,
        expected_branch_ref=_BRANCH_REF,
        command_evidence=("pytest -q",),
    )


@pytest.mark.unit
async def test_missing_head_filesystem_recovery_returns_none_without_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    runner = _RecoveryRunner(cmd)
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: None)

    assert await _recover(runner, worktree) is None
    assert cmd.calls == []


@pytest.mark.unit
async def test_protected_scope_repair_checks_head_after_unexpected_adapter_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    worktrees_root = tmp_path / "worktrees"
    worktree = worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)
    runner = _ProtectedRepairRunner(cmd, worktrees_root=worktrees_root)
    mirror = tmp_path / "mirror.git"
    calls: list[str] = []

    async def _repair_hooks(_mirror: Path) -> None:
        calls.append("repair_hooks")

    async def _head_missing(_worktree_path: Path) -> bool:
        calls.append("verify_head")
        return False

    async def _repair_ownership(**_kwargs: object) -> bool:
        calls.append("repair_ownership")
        return True

    monkeypatch.setattr(
        pr_remote_repair_protected,
        "mirror_path_for_worktree",
        lambda _path: mirror,
    )
    monkeypatch.setattr(pr_remote_repair_protected, "repair_mirror_hooks_path", _repair_hooks)
    monkeypatch.setattr(pr_remote_repair_protected, "verify_head_object_exists", _head_missing)
    monkeypatch.setattr(
        pr_remote_repair_protected,
        "repair_agent_runtime_ownership",
        _repair_ownership,
    )

    with pytest.raises(_MonitorHeadObjectMissingError, match="after protected-scope repair"):
        await pr_remote_repair_protected._repair_protected_scope_changes_before_commit(
            runner,
            workspace_id=_WORKSPACE_ID,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    assert calls == ["repair_ownership", "repair_hooks", "repair_hooks", "verify_head"]
    assert runner.violations_calls == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("queued_results", "branch_ref", "expected_command_suffix"),
    (
        (
            [(1, "", "missing start")],
            _BRANCH_REF,
            ["cat-file", "-e", f"{_START_HEAD}^{{commit}}"],
        ),
        (
            [(0, "", "")],
            None,
            ["cat-file", "-e", f"{_START_HEAD}^{{commit}}"],
        ),
        (
            [(0, "", ""), (1, "", "update failed")],
            _BRANCH_REF,
            ["update-ref", _BRANCH_REF, _START_HEAD],
        ),
        (
            [(0, "", ""), (0, "", ""), (1, "", "reset failed"), (1, "", "cleanup failed")],
            _BRANCH_REF,
            ["reset", "--hard", _START_HEAD],
        ),
        (
            [(0, "", ""), (0, "", ""), (0, "", ""), (1, "", "add failed"), (0, "", "")],
            _BRANCH_REF,
            ["reset", "--hard", _START_HEAD],
        ),
        (
            [
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "", ""),
                (1, "", "diff failed"),
                (0, "", ""),
            ],
            _BRANCH_REF,
            ["reset", "--hard", _START_HEAD],
        ),
    ),
)
async def test_missing_head_filesystem_recovery_aborts_failed_git_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    queued_results: tuple[tuple[int, str, str], ...],
    branch_ref: str | None,
    expected_command_suffix: list[str],
) -> None:
    cmd = FakeCommandRunner()
    for returncode, stdout, stderr in queued_results:
        cmd.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    runner = _RecoveryRunner(cmd)
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _resolve_branch(_worktree_path: Path) -> str | None:
        return branch_ref

    monkeypatch.setattr(pr_remote_repair, "_resolve_worktree_branch_ref", _resolve_branch)

    assert await _recover(runner, worktree) is None
    assert cmd.calls[-1].args[-len(expected_command_suffix) :] == expected_command_suffix


@pytest.mark.unit
async def test_missing_head_filesystem_recovery_rejects_branch_ref_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = _RecoveryRunner(cmd)
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _resolve_branch(_worktree_path: Path) -> str:
        return "refs/heads/main"

    monkeypatch.setattr(pr_remote_repair, "_resolve_worktree_branch_ref", _resolve_branch)

    assert await _recover(runner, worktree) is None
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_missing_head_filesystem_recovery_cleans_up_runtime_unstage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="A\0.claude/agent-memory/reviewer.json\0")
    cmd.queue_result(returncode=1, stderr="cannot unstage runtime file")
    cmd.queue_result(returncode=0)
    runner = _RecoveryRunner(cmd)
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _resolve_branch(_worktree_path: Path) -> str:
        return _BRANCH_REF

    monkeypatch.setattr(pr_remote_repair, "_resolve_worktree_branch_ref", _resolve_branch)

    assert await _recover(runner, worktree) is None
    assert any(
        call.args[-6:]
        == [
            "--literal-pathspecs",
            "reset",
            "-q",
            "HEAD",
            "--",
            ".claude/agent-memory/reviewer.json",
        ]
        for call in cmd.calls
    )


@pytest.mark.unit
async def test_missing_head_filesystem_recovery_cleans_up_policy_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="A\0src/awf/new_module.py\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1, stderr="clean failed")
    runner = _RecoveryRunner(cmd, policy_message="protected workflow changed")
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _resolve_branch(_worktree_path: Path) -> str:
        return _BRANCH_REF

    monkeypatch.setattr(pr_remote_repair, "_resolve_worktree_branch_ref", _resolve_branch)

    with pytest.raises(_MonitorPolicyBlockedError, match="protected workflow changed"):
        await _recover(runner, worktree)

    assert runner.policy_calls == [("src/awf/new_module.py",)]
    assert cmd.calls[-1].args[-5:] == [
        "--literal-pathspecs",
        "clean",
        "-fd",
        "--",
        "src/awf/new_module.py",
    ]


@pytest.mark.unit
async def test_missing_head_filesystem_recovery_commits_filesystem_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="M\0src/awf/runtime/pr_monitor_runner/remote_repair.py\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=f"{_RECOVERED_HEAD}\n")
    cmd.queue_result(returncode=0)
    runner = _RecoveryRunner(cmd)
    mirror = tmp_path / "mirror.git"
    worktree = tmp_path / "worktree"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)
    monkeypatch.setattr(pr_remote_repair, "repair_agent_writable_worktree", lambda *_args: None)

    async def _resolve_branch(_worktree_path: Path) -> str:
        return _BRANCH_REF

    monkeypatch.setattr(pr_remote_repair, "_resolve_worktree_branch_ref", _resolve_branch)

    assert await _recover(runner, worktree, task_tag="AWF-123") == _RECOVERED_HEAD
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any("commit -m AWF-123 awf: recover ws_recovery_edges" in call for call in joined_calls)
    assert runner.policy_calls == [("src/awf/runtime/pr_monitor_runner/remote_repair.py",)]


@pytest.mark.unit
def test_worktree_git_dir_handles_directory_missing_bad_and_relative_gitdirs(
    tmp_path: Path,
) -> None:
    direct = tmp_path / "direct"
    direct_git = direct / ".git"
    direct_git.mkdir(parents=True)
    assert pr_remote_repair._worktree_git_dir(direct) == direct_git

    missing = tmp_path / "missing"
    missing.mkdir()
    assert pr_remote_repair._worktree_git_dir(missing) is None

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".git").write_text("not a gitdir file", encoding="utf-8")
    assert pr_remote_repair._worktree_git_dir(bad) is None

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../mirror/worktrees/ws\n", encoding="utf-8")
    assert pr_remote_repair._worktree_git_dir(linked) == linked / "../mirror/worktrees/ws"


@pytest.mark.unit
async def test_commit_dirty_worktree_rejects_malformed_recovered_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M\0src/awf/runtime/pr_monitor_runner/remote_repair.py")
    worktrees_root = tmp_path / "worktrees"
    worktree = worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)
    runner = _CommitRunner(cmd, worktrees_root=worktrees_root)
    mirror = tmp_path / "mirror.git"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _repair_hooks(_mirror: Path) -> None:
        return None

    monkeypatch.setattr(pr_remote_repair, "repair_mirror_hooks_path", _repair_hooks)

    async def _head_missing(_worktree_path: Path) -> bool:
        return False

    async def _anchor_exists(_self: object, _mirror_path: Path, _commit_sha: str) -> bool:
        return True

    async def _recover_head(*_args: object, **_kwargs: object) -> str:
        return _RECOVERED_HEAD

    monkeypatch.setattr(pr_remote_repair, "verify_head_object_exists", _head_missing)
    monkeypatch.setattr(pr_remote_repair, "_mirror_commit_object_exists", _anchor_exists)
    monkeypatch.setattr(
        pr_remote_repair, "_recover_missing_head_object_from_filesystem", _recover_head
    )

    with pytest.raises(_MonitorHeadObjectMissingError, match="recovered diff was malformed"):
        await pr_remote_repair._commit_dirty_worktree(
            runner,
            workspace_id=_WORKSPACE_ID,
            message="fix: recovered",
            operation_start_head=_START_HEAD,
            task_tag=None,
        )


@pytest.mark.unit
async def test_commit_dirty_worktree_returns_false_when_recovered_repair_status_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M\0src/awf/runtime/pr_monitor_runner/remote_repair.py\0")
    cmd.queue_result(returncode=1, stderr="status failed")
    worktrees_root = tmp_path / "worktrees"
    worktree = worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)
    runner = _CommitRunner(cmd, worktrees_root=worktrees_root)
    mirror = tmp_path / "mirror.git"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    async def _repair_hooks(_mirror: Path) -> None:
        return None

    monkeypatch.setattr(pr_remote_repair, "repair_mirror_hooks_path", _repair_hooks)

    async def _head_missing(_worktree_path: Path) -> bool:
        return False

    async def _anchor_exists(_self: object, _mirror_path: Path, _commit_sha: str) -> bool:
        return True

    async def _recover_head(*_args: object, **_kwargs: object) -> str:
        return _RECOVERED_HEAD

    async def _repair_ownership(**_kwargs: object) -> bool:
        return True

    async def _no_violations(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(pr_remote_repair, "verify_head_object_exists", _head_missing)
    monkeypatch.setattr(pr_remote_repair, "_mirror_commit_object_exists", _anchor_exists)
    monkeypatch.setattr(
        pr_remote_repair, "_recover_missing_head_object_from_filesystem", _recover_head
    )
    monkeypatch.setattr(pr_remote_repair, "repair_agent_runtime_ownership", _repair_ownership)
    monkeypatch.setattr(
        pr_remote_repair,
        "_protected_scope_violations_for_recovered_dirty_commit",
        _no_violations,
    )

    result = await pr_remote_repair._commit_dirty_worktree(
        runner,
        workspace_id=_WORKSPACE_ID,
        message="fix: recovered",
        operation_start_head=_START_HEAD,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        task_tag=None,
    )

    assert result is False
    assert runner.protected_repair_calls == 1
    post_repair_status_call = next(
        call
        for call in cmd.calls
        if call.args[-3:] == ["status", "--porcelain", "--untracked-files=all"]
    )
    assert post_repair_status_call.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in post_repair_status_call.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in post_repair_status_call.env


@pytest.mark.unit
async def test_commit_dirty_worktree_maps_mirror_hook_oserror_to_terminal_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    worktrees_root = tmp_path / "worktrees"
    worktree = worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)
    runner = _CommitRunner(cmd, worktrees_root=worktrees_root)
    mirror = tmp_path / "mirror.git"
    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: mirror)

    def _raise_oserror(_mirror: Path) -> None:
        raise OSError("hooks path unreadable")

    monkeypatch.setattr(pr_remote_repair, "repair_mirror_hooks_path", _raise_oserror)

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await pr_remote_repair._commit_dirty_worktree(
            runner,
            workspace_id=_WORKSPACE_ID,
            message="fix: hooks",
            operation_start_head=_START_HEAD,
            task_tag=None,
        )
