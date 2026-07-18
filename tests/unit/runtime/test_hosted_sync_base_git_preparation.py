"""Focused hosted SyncBase git-preparation contract regressions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.adapters.runtime_executor import AgentRuntimeGitPreparation
from awf.common.commands import CommandResult
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor_runner import agent_service_recovery, remote_ops
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

_START_HEAD = "a" * 40
_BASE_SHA = "b" * 40
_TERMINAL_HEAD = "c" * 40


class _SyncCommandRunner:
    def __init__(
        self,
        *,
        base_result: CommandResult | None = None,
        terminal_head: str = _TERMINAL_HEAD,
    ) -> None:
        self.base_result = base_result or CommandResult(0, f"{_BASE_SHA}\n", "")
        self.terminal_head = terminal_head
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.conflicted_index = False

    async def run(
        self,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
        **_kwargs: object,
    ) -> CommandResult:
        self.calls.append(args)
        self.envs.append(env)
        command = args[args.index("-C") + 2 :]
        if command == ["merge", "--abort"]:
            self.conflicted_index = False
            return CommandResult(0, "", "")
        if command[:2] == ["merge", "--no-edit"]:
            self.conflicted_index = True
            return CommandResult(1, "", "CONFLICT (content): merge conflict")
        if command == ["status", "--porcelain"]:
            return CommandResult(0, "UU src/conflict.py\n", "")
        if command == ["rev-parse", "origin/development"]:
            return self.base_result
        if command == [
            "fetch",
            "--no-tags",
            "git@github.com:example/project.git",
            "feature/ready",
        ]:
            return CommandResult(0, "", "")
        if command == ["rev-parse", "FETCH_HEAD"]:
            return CommandResult(0, f"{self.terminal_head}\n", "")
        if command == ["reset", "--hard", self.terminal_head]:
            self.conflicted_index = False
            return CommandResult(0, "reset\n", "")
        if command == [
            "diff",
            "--name-status",
            "-z",
            f"{_START_HEAD}..{self.terminal_head}",
            "--",
        ]:
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected command: {command!r}")


class _HostedAdapter:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self, terminal_head: str = _TERMINAL_HEAD) -> None:
        self.terminal_head = terminal_head
        self.calls: list[dict[str, object]] = []

    async def run(self, **kwargs: object) -> AgentRunResult:
        self.calls.append(dict(kwargs))
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: remote conflict repair",
            stderr="",
            terminal_head_sha=self.terminal_head,
        )


class _LocalAdapter:
    name = AgentRuntime.codex
    is_hosted = False


class _SyncBaseHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        adapter: object,
        command_runner: _SyncCommandRunner,
        actual_hosted_recovery: bool = False,
    ) -> None:
        self._worktrees_root = tmp_path
        self._workspace_runtime_context = ""
        self._deps = SimpleNamespace(runner=command_runner, adapter=adapter)
        self.actual_hosted_recovery = actual_hosted_recovery
        self.agent_calls: list[dict[str, object]] = []
        self.commit_sink_conflict_states: list[bool] = []

    async def _repair_operation_start_head_result(self, **_kwargs: object) -> tuple[str, None]:
        return _START_HEAD, None

    async def _resolve_task_tag(self, _workspace_id: str) -> None:
        return None

    async def _fetch_base(self, **_kwargs: object) -> None:
        return None

    async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
        return False

    async def _run_monitor_agent_with_service_recovery(self, **kwargs: object) -> AgentRunResult:
        self.agent_calls.append(dict(kwargs))
        if self.actual_hosted_recovery:
            return await agent_service_recovery._run_monitor_agent_with_service_recovery(
                self,
                **kwargs,
            )
        return AgentRunResult(returncode=0, stdout="fixed", stderr="")

    async def _commit_dirty_worktree(self, **_kwargs: object) -> None:
        self.commit_sink_conflict_states.append(self._deps.runner.conflicted_index)

    async def _protected_scope_push_block(self, **_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(self, **_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _load_workspace(self, _workspace_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            repo_url="git@github.com:example/project.git",
            pr_url="https://github.com/example/project/pull/17",
            pr_number=17,
            branch_base="development",
            remote_push_branch="feature/ready",
            owned_paths=[],
            monitor_last_commit_sha=_START_HEAD,
            task_policy={
                "pr_adoption": {
                    "base_ref": "development",
                    "head_ref": "feature/ready",
                    "head_repo_url": "git@github.com:example/project.git",
                    "head_repo_slug": "example/project",
                    "head_sha": _START_HEAD,
                }
            },
        )


@pytest.fixture(autouse=True)
def _allow_runtime_ownership_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _repair(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_ops, "repair_agent_runtime_ownership", _repair)


async def _run_conflicted_sync(harness: _SyncBaseHarness) -> _GitPushResult:
    return await remote_ops._run_sync_base(
        harness,  # type: ignore[arg-type]
        workspace_id="ws_hosted_conflict",
        repo=RepoRef(owner="example", name="project"),
        pr_number=17,
        pr_head_sha=_START_HEAD,
        base_branch="development",
        remote_branch="feature/ready",
        compose_project="awf_ws_hosted_conflict",
        compose_file=Path("/tmp/missing-compose.yml"),
    )


@pytest.mark.unit
async def test_hosted_sync_base_conflict_emits_exact_pinned_git_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    command_runner = _SyncCommandRunner()
    harness = _SyncBaseHarness(
        tmp_path,
        adapter=_HostedAdapter(),
        command_runner=command_runner,
    )

    result = await _run_conflicted_sync(harness)

    assert result.pushed is True
    assert harness.agent_calls[0]["git_preparation"] == AgentRuntimeGitPreparation(
        mode="merge_base",
        base_ref="development",
        expected_base_sha=_BASE_SHA,
    )
    rev_parse_index = next(
        index
        for index, call in enumerate(command_runner.calls)
        if call[-2:] == ["rev-parse", "origin/development"]
    )
    rev_parse_env = command_runner.envs[rev_parse_index]
    assert rev_parse_env is not None
    assert "GIT_OBJECT_DIRECTORY" not in rev_parse_env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in rev_parse_env


@pytest.mark.unit
async def test_local_sync_base_conflict_preserves_local_conflicted_worktree_flow(
    tmp_path: Path,
) -> None:
    command_runner = _SyncCommandRunner()
    harness = _SyncBaseHarness(
        tmp_path,
        adapter=_LocalAdapter(),
        command_runner=command_runner,
    )

    result = await _run_conflicted_sync(harness)

    assert result.pushed is True
    assert "git_preparation" not in harness.agent_calls[0]
    assert not any(
        call[-2:] == ["rev-parse", "origin/development"] for call in command_runner.calls
    )
    assert harness.commit_sink_conflict_states == [True]


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_result",
    [
        CommandResult(1, "", "fatal: unknown revision"),
        CommandResult(0, "\n", ""),
        CommandResult(0, "b" * 39, ""),
        CommandResult(0, "B" * 40, ""),
        CommandResult(0, "g" * 40, ""),
        CommandResult(0, f"{'b' * 40}\n{'c' * 40}\n", ""),
    ],
    ids=("command-failure", "blank", "short", "uppercase", "non-hex", "multiline"),
)
async def test_hosted_sync_base_conflict_fails_closed_for_unusable_base_sha(
    tmp_path: Path,
    base_result: CommandResult,
) -> None:
    command_runner = _SyncCommandRunner(base_result=base_result)
    adapter = _HostedAdapter()
    harness = _SyncBaseHarness(tmp_path, adapter=adapter, command_runner=command_runner)

    result = await _run_conflicted_sync(harness)

    assert result.failed is True
    assert result.pushed is False
    assert result.reason_code == "SYNC_BASE_GIT_PREPARATION_FAILED"
    assert result.terminal_monitor_failure is True
    assert result.details == {
        "base_ref": "development",
        "remote_ref": "origin/development",
    }
    assert harness.agent_calls == []
    assert adapter.calls == []


@pytest.mark.unit
async def test_hosted_conflict_advanced_head_resets_before_dirty_commit_sink(
    tmp_path: Path,
) -> None:
    command_runner = _SyncCommandRunner()
    adapter = _HostedAdapter()
    harness = _SyncBaseHarness(
        tmp_path,
        adapter=adapter,
        command_runner=command_runner,
        actual_hosted_recovery=True,
    )

    result = await _run_conflicted_sync(harness)

    assert result.pushed is True
    assert adapter.calls[0]["git_preparation"] == AgentRuntimeGitPreparation(
        mode="merge_base",
        base_ref="development",
        expected_base_sha=_BASE_SHA,
    )
    assert harness.commit_sink_conflict_states == [False]
    commands = [call[call.index("-C") + 2 :] for call in command_runner.calls]
    reset_index = commands.index(["reset", "--hard", _TERMINAL_HEAD])
    delta_index = commands.index(
        ["diff", "--name-status", "-z", f"{_START_HEAD}..{_TERMINAL_HEAD}", "--"]
    )
    assert reset_index < delta_index
    assert not any(command and command[0] == "commit" for command in commands)
