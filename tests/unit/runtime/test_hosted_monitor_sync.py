"""Hosted PR monitor terminal-head synchronization tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.agent_service_recovery import (
    _hosted_pr_identity_for_workspace,
    _run_monitor_agent_with_service_recovery,
    _sync_hosted_worktree_to_terminal_head,
)


class _Runner:
    def __init__(self, *, fetched_sha: str) -> None:
        self.fetched_sha = fetched_sha
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    async def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(args)
        self.envs.append(env)
        if args[-1] == "feature/ready":
            return CommandResult(returncode=0, stdout="", stderr="")
        if args[-1] == "FETCH_HEAD":
            return CommandResult(returncode=0, stdout=self.fetched_sha + "\n", stderr="")
        if args[-2:] == ["--hard", self.fetched_sha]:
            return CommandResult(returncode=0, stdout="reset\n", stderr="")
        return CommandResult(returncode=1, stdout="", stderr="unexpected command")


class _HostedAdapterWithoutTerminalHead:
    name = AgentRuntime.codex
    is_hosted = True

    async def run(self, **_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="done", stderr="", terminal_head_sha=None)


class _HostedAdapterWithTerminalHead:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self, terminal_head_sha: str) -> None:
        self.terminal_head_sha = terminal_head_sha
        self.hosted_pr_identities: list[dict[str, object] | None] = []

    async def run(self, **kwargs: object) -> AgentRunResult:
        hosted_pr_identity = kwargs.get("hosted_pr_identity")
        identity = hosted_pr_identity if isinstance(hosted_pr_identity, dict) else None
        self.hosted_pr_identities.append(identity)
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: remote repair",
            stderr="",
            terminal_head_sha=self.terminal_head_sha,
        )


def _runner_context(tmp_path: Path, runner: _Runner) -> SimpleNamespace:
    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(
            runner=runner,
            adapter=SimpleNamespace(name=AgentRuntime.codex),
        ),
    )


def _monitor_context_with_runner(
    tmp_path: Path,
    *,
    runner: _Runner,
    adapter: object,
) -> SimpleNamespace:
    context = _monitor_context_with_adapter(adapter)
    context._worktrees_root = tmp_path
    context._deps.runner = runner
    return context


def _monitor_context_with_adapter(adapter: object) -> SimpleNamespace:
    async def _load_workspace(_workspace_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            repo_url="git@github.com:dimileeh/aira-web.git",
            pr_url="https://github.com/dimileeh/aira-web/pull/751",
            pr_number=751,
            branch_base="main",
            remote_push_branch="feature/ready",
            owned_paths=[],
            monitor_last_commit_sha="a" * 40,
            task_policy={
                "pr_adoption": {
                    "base_ref": "main",
                    "head_ref": "feature/ready",
                    "head_sha": "a" * 40,
                }
            },
        )

    return SimpleNamespace(
        _load_workspace=_load_workspace,
        _deps=SimpleNamespace(adapter=adapter),
    )


@pytest.mark.unit
async def test_sync_hosted_worktree_fetches_and_resets_terminal_head(tmp_path: Path) -> None:
    sha = "b" * 40
    runner = _Runner(fetched_sha=sha)

    await _sync_hosted_worktree_to_terminal_head(
        _runner_context(tmp_path, runner),
        workspace_id="ws_hosted",
        hosted_pr_identity={
            "head_repo_url": "git@github.com:dimileeh/aira-web.git",
            "head_ref": "feature/ready",
        },
        terminal_head_sha=sha,
    )

    assert runner.calls == [
        [
            "git",
            "-C",
            str(tmp_path / "ws_hosted"),
            "fetch",
            "--no-tags",
            "git@github.com:dimileeh/aira-web.git",
            "feature/ready",
        ],
        ["git", "-C", str(tmp_path / "ws_hosted"), "rev-parse", "FETCH_HEAD"],
        ["git", "-C", str(tmp_path / "ws_hosted"), "reset", "--hard", sha],
    ]


@pytest.mark.unit
async def test_sync_hosted_worktree_scrubs_git_object_lookup_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    monkeypatch.setenv("AWF_HOSTED_SYNC_TEST_ENV", "preserved")
    sha = "b" * 40
    runner = _Runner(fetched_sha=sha)

    await _sync_hosted_worktree_to_terminal_head(
        _runner_context(tmp_path, runner),
        workspace_id="ws_hosted",
        hosted_pr_identity={
            "head_repo_url": "git@github.com:dimileeh/aira-web.git",
            "head_ref": "feature/ready",
        },
        terminal_head_sha=sha,
    )

    assert len(runner.envs) == 3
    for env in runner.envs:
        assert env is not None
        assert "GIT_OBJECT_DIRECTORY" not in env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env
        assert env["AWF_HOSTED_SYNC_TEST_ENV"] == "preserved"


@pytest.mark.unit
async def test_sync_hosted_worktree_accepts_uppercase_terminal_head_sha(
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    runner = _Runner(fetched_sha=sha)

    await _sync_hosted_worktree_to_terminal_head(
        _runner_context(tmp_path, runner),
        workspace_id="ws_hosted",
        hosted_pr_identity={
            "head_repo_url": "git@github.com:dimileeh/aira-web.git",
            "head_ref": "feature/ready",
        },
        terminal_head_sha=sha.upper(),
    )

    assert runner.calls[-1] == [
        "git",
        "-C",
        str(tmp_path / "ws_hosted"),
        "reset",
        "--hard",
        sha,
    ]


@pytest.mark.unit
async def test_sync_hosted_worktree_rejects_remote_head_mismatch(tmp_path: Path) -> None:
    runner = _Runner(fetched_sha="c" * 40)

    with pytest.raises(AgentRunError) as excinfo:
        await _sync_hosted_worktree_to_terminal_head(
            _runner_context(tmp_path, runner),
            workspace_id="ws_hosted",
            hosted_pr_identity={
                "head_repo_url": "git@github.com:dimileeh/aira-web.git",
                "head_ref": "feature/ready",
            },
            terminal_head_sha="b" * 40,
        )

    assert excinfo.value.reason_code == "HOSTED_REMOTE_HEAD_MISMATCH"


@pytest.mark.unit
async def test_hosted_agent_success_without_terminal_head_fails_closed() -> None:
    with pytest.raises(AgentRunError) as excinfo:
        await _run_monitor_agent_with_service_recovery(
            _monitor_context_with_adapter(_HostedAdapterWithoutTerminalHead()),
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=Path("/tmp/missing-compose.yml"),
            prompt="fix review",
            log_source="monitor",
        )

    assert excinfo.value.reason_code == "HOSTED_REMOTE_HEAD_MISSING"


@pytest.mark.unit
async def test_hosted_agent_sync_advances_monitor_state_after_terminal_head(
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    runner = _Runner(fetched_sha=sha)
    adapter = _HostedAdapterWithTerminalHead(sha.upper())
    state = MonitorState(last_push_sha="a" * 40)
    context = _monitor_context_with_runner(tmp_path, runner=runner, adapter=adapter)

    result = await _run_monitor_agent_with_service_recovery(
        context,
        workspace_id="ws_hosted",
        compose_project="awf_ws_hosted",
        compose_file=Path("/tmp/missing-compose.yml"),
        prompt="fix review",
        log_source="monitor",
        state=state,
    )

    assert result.terminal_head_sha == sha.upper()
    assert state.last_push_sha == sha
    assert adapter.hosted_pr_identities[0]["expected_head_sha"] == "a" * 40
    refreshed_identity = await _hosted_pr_identity_for_workspace(
        context,
        "ws_hosted",
        state=state,
    )
    assert refreshed_identity["expected_head_sha"] == sha
