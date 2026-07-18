"""Focused hosted SyncBase merge-preparation contract regressions."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.adapters.runtime_executor import AgentRuntimeGitPreparation
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.git_identity import (
    DEFAULT_GIT_AUTHOR_EMAIL,
    DEFAULT_GIT_AUTHOR_NAME,
    git_identity_config_args,
)
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor_runner import agent_service_recovery, remote_ops
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

_START_HEAD = "a" * 40
_BASE_SHA = "b" * 40
_TERMINAL_HEAD = "c" * 40
_ADVANCED_BASE_SHA = "d" * 40


class _SyncCommandRunner:
    def __init__(
        self,
        *,
        base_result: CommandResult | None = None,
        merge_result: CommandResult | None = None,
        status_result: CommandResult | None = None,
        remote_base_sha: str | None = None,
        terminal_head: str = _TERMINAL_HEAD,
    ) -> None:
        self.base_result = base_result or CommandResult(0, f"{_BASE_SHA}\n", "")
        self.merge_result = merge_result or CommandResult(
            1, "", "CONFLICT (content): merge conflict"
        )
        self.status_result = status_result
        self.remote_base_sha = remote_base_sha
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
        while command[:1] == ["-c"]:
            command = command[2:]
        if command == ["merge", "--abort"]:
            self.conflicted_index = False
            return CommandResult(0, "", "")
        if command[:2] == ["merge", "--no-edit"]:
            self.conflicted_index = True
            return self.merge_result
        if command == ["status", "--porcelain"]:
            if self.status_result is not None:
                return self.status_result
            return CommandResult(0, "UU src/conflict.py\n", "")
        if command == ["rev-parse", "MERGE_HEAD"]:
            return self.base_result
        if command == ["rev-parse", "origin/development"]:
            if self.remote_base_sha is None:
                return self.base_result
            return CommandResult(0, f"{self.remote_base_sha}\n", "")
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


class _RecordingRealCommandRunner:
    def __init__(self) -> None:
        self._runner = AsyncioSubprocessRunner()
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []

    async def run(
        self,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> CommandResult:
        self.calls.append(args)
        self.envs.append(env)
        return await self._runner.run(args, env=env, **kwargs)


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
        command_runner: object,
        actual_hosted_recovery: bool = False,
        task_tag: str | None = None,
    ) -> None:
        self._worktrees_root = tmp_path
        self._workspace_runtime_context = ""
        self._deps = SimpleNamespace(runner=command_runner, adapter=adapter)
        self.actual_hosted_recovery = actual_hosted_recovery
        self.task_tag = task_tag
        self.agent_calls: list[dict[str, object]] = []
        self.commit_sink_conflict_states: list[bool] = []
        self.validated_push_calls = 0

    async def _repair_operation_start_head_result(self, **_kwargs: object) -> tuple[str, None]:
        return _START_HEAD, None

    async def _resolve_task_tag(self, _workspace_id: str) -> str | None:
        return self.task_tag

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
        self.commit_sink_conflict_states.append(
            bool(getattr(self._deps.runner, "conflicted_index", False))
        )

    async def _protected_scope_push_block(self, **_kwargs: object) -> None:
        return None

    async def _validated_git_push_result(self, **_kwargs: object) -> _GitPushResult:
        self.validated_push_calls += 1
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


def _git(
    worktree: Path,
    *args: str,
    env: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _seed_cleanly_mergeable_histories(
    worktree: Path,
    *,
    env: Mapping[str, str],
) -> str:
    worktree.mkdir()
    _git(worktree, "init", "-q", "--initial-branch=feature/ready", env=env)
    (worktree / "shared.txt").write_text("shared\n", encoding="utf-8")
    _git(worktree, "add", "shared.txt", env=env)
    _git(
        worktree,
        *git_identity_config_args(name="Fixture Author", email="fixture@example.com"),
        "commit",
        "-qm",
        "root",
        env=env,
    )
    root_sha = _git(worktree, "rev-parse", "HEAD", env=env).stdout.strip()

    base_index = worktree.parent / "base.index"
    base_env = dict(env)
    base_env["GIT_INDEX_FILE"] = str(base_index)
    _git(worktree, "read-tree", root_sha, env=base_env)
    (worktree / "base.txt").write_text("base\n", encoding="utf-8")
    _git(worktree, "add", "base.txt", env=base_env)
    base_tree = _git(worktree, "write-tree", env=base_env).stdout.strip()
    base_sha = _git(
        worktree,
        *git_identity_config_args(name="Fixture Author", email="fixture@example.com"),
        "commit-tree",
        base_tree,
        "-p",
        root_sha,
        "-m",
        "base change",
        env=base_env,
    ).stdout.strip()
    (worktree / "base.txt").unlink()
    base_index.unlink()
    _git(worktree, "update-ref", "refs/remotes/origin/development", base_sha, env=env)

    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(worktree, "add", "feature.txt", env=env)
    _git(
        worktree,
        *git_identity_config_args(name="Fixture Author", email="fixture@example.com"),
        "commit",
        "-qm",
        "feature change",
        env=env,
    )
    return base_sha


@pytest.mark.unit
async def test_clean_sync_base_merge_uses_command_scoped_awf_identity_without_delegation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    global_config = tmp_path / "global.gitconfig"
    system_config = tmp_path / "system.gitconfig"
    global_config.touch()
    system_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    isolated_git_env = dict(os.environ)

    worktree = tmp_path / "ws_hosted_conflict"
    base_sha = _seed_cleanly_mergeable_histories(worktree, env=isolated_git_env)
    inherited_identity = {
        "GIT_AUTHOR_NAME": "Ambient Author",
        "GIT_AUTHOR_EMAIL": "ambient-author@example.com",
        "GIT_COMMITTER_NAME": "Ambient Committer",
        "GIT_COMMITTER_EMAIL": "ambient-committer@example.com",
    }
    for key, value in inherited_identity.items():
        monkeypatch.setenv(key, value)
    command_runner = _RecordingRealCommandRunner()
    adapter = _HostedAdapter()
    harness = _SyncBaseHarness(
        tmp_path,
        adapter=adapter,
        command_runner=command_runner,
        task_tag="SYNC-4",
    )

    result = await _run_conflicted_sync(harness)

    assert result.pushed is True
    parents = _git(worktree, "show", "-s", "--format=%P", "HEAD", env=isolated_git_env)
    assert len(parents.stdout.split()) == 2
    assert base_sha in parents.stdout.split()
    metadata = (
        _git(
            worktree,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce%x00%s",
            "HEAD",
            env=isolated_git_env,
        )
        .stdout.rstrip("\n")
        .split("\0")
    )
    assert metadata == [
        DEFAULT_GIT_AUTHOR_NAME,
        DEFAULT_GIT_AUTHOR_EMAIL,
        DEFAULT_GIT_AUTHOR_NAME,
        DEFAULT_GIT_AUTHOR_EMAIL,
        "SYNC-4 Merge remote-tracking branch 'origin/development'",
    ]
    local_name = _git(
        worktree,
        "config",
        "--local",
        "--get",
        "user.name",
        env=isolated_git_env,
        check=False,
    )
    assert local_name.returncode == 1
    merge_call = next(call for call in command_runner.calls if "--no-edit" in call)
    merge_call_index = command_runner.calls.index(merge_call)
    merge_index = merge_call.index("merge")
    assert merge_call[merge_index - 4 : merge_index] == git_identity_config_args()
    merge_env = command_runner.envs[merge_call_index]
    assert merge_env is not None
    assert inherited_identity.keys().isdisjoint(merge_env)
    assert harness.agent_calls == []
    assert adapter.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("adapter_kind", ["hosted", "local"])
@pytest.mark.parametrize(
    "status_result",
    [
        CommandResult(0, "", ""),
        CommandResult(0, " M src/dirty.py\n?? scratch.txt\n", ""),
        CommandResult(
            73,
            "",
            "fatal: token=ghp_abcdefghijklmnopqrstuvwxyz1234567890 while inspecting index",
        ),
    ],
    ids=("clean-status", "ordinary-changes", "status-failure"),
)
async def test_non_conflict_sync_base_merge_failure_fails_closed_without_agent_or_push(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter_kind: str,
    status_result: CommandResult,
) -> None:
    def _unexpected_prompt(**_kwargs: object) -> str:
        raise AssertionError("non-conflict merge failure must not build an agent prompt")

    monkeypatch.setattr(
        "awf.runtime.monitor_prompts.sync_base_conflict_prompt",
        _unexpected_prompt,
    )
    merge_result = CommandResult(
        42,
        "",
        "fatal: https://build:ghp_abcdefghijklmnopqrstuvwxyz1234567890@github.com/" + "x" * 2500,
    )
    command_runner = _SyncCommandRunner(
        merge_result=merge_result,
        status_result=status_result,
    )
    adapter: object = _HostedAdapter() if adapter_kind == "hosted" else _LocalAdapter()
    harness = _SyncBaseHarness(tmp_path, adapter=adapter, command_runner=command_runner)

    result = await _run_conflicted_sync(harness)

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 42
    assert result.reason_code == "SYNC_BASE_MERGE_FAILED"
    assert result.terminal_monitor_failure is True
    assert result.details == {
        "base_ref": "development",
        "merge_returncode": 42,
        "status_returncode": status_result.returncode,
    }
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in result.stderr
    assert "[redacted]" in result.stderr
    assert len(result.stderr) <= 2014
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in repr(result.details)
    assert harness.agent_calls == []
    assert harness.commit_sink_conflict_states == []
    assert harness.validated_push_calls == 0


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
        if call[-2:] == ["rev-parse", "MERGE_HEAD"]
    )
    rev_parse_env = command_runner.envs[rev_parse_index]
    assert rev_parse_env is not None
    assert "GIT_OBJECT_DIRECTORY" not in rev_parse_env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in rev_parse_env


@pytest.mark.unit
async def test_hosted_sync_base_pins_failed_merge_when_remote_ref_advances(
    tmp_path: Path,
) -> None:
    command_runner = _SyncCommandRunner(remote_base_sha=_ADVANCED_BASE_SHA)
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
    assert any(call[-2:] == ["rev-parse", "MERGE_HEAD"] for call in command_runner.calls)
    assert not any(
        call[-2:] == ["rev-parse", "origin/development"] for call in command_runner.calls
    )


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
        "merge_ref": "MERGE_HEAD",
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
