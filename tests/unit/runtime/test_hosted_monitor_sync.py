"""Hosted PR monitor terminal-head synchronization tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.adapters.runtime_executor import AgentRuntimeGitPreparation
from awf.common.commands import CommandResult
from awf.common.git_identity import git_safe_directory_config_args
from awf.control.executor.monitor_handoff import _hosted_handoff_pr_identity
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner.agent_service_recovery import (
    _hosted_pr_identity_for_workspace,
    _rollback_hosted_terminal_head_on_remote,
    _run_monitor_agent_with_service_recovery,
    _sync_hosted_worktree_to_terminal_head,
)


class _Runner:
    def __init__(
        self,
        *,
        fetched_sha: str,
        current_sha: str = "a" * 40,
        diff_stdout: str = "",
        fetch_returncode: int = 0,
        fetch_stderr: str = "",
    ) -> None:
        self.fetched_sha = fetched_sha
        self.current_sha = current_sha
        self.diff_stdout = diff_stdout
        self.fetch_returncode = fetch_returncode
        self.fetch_stderr = fetch_stderr
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
        if args[-2:] == ["rev-parse", "HEAD"]:
            return CommandResult(returncode=0, stdout=self.current_sha + "\n", stderr="")
        if args[-1] == "feature/ready":
            return CommandResult(
                returncode=self.fetch_returncode,
                stdout="",
                stderr=self.fetch_stderr,
            )
        if args[-1] == "FETCH_HEAD":
            return CommandResult(returncode=0, stdout=self.fetched_sha + "\n", stderr="")
        if args[-2:] == ["--hard", self.fetched_sha]:
            return CommandResult(returncode=0, stdout="reset\n", stderr="")
        if args[-5:-2] == ["diff", "--name-status", "-z"] and args[-1] == "--":
            return CommandResult(returncode=0, stdout=self.diff_stdout, stderr="")
        return CommandResult(returncode=1, stdout="", stderr="unexpected command")


class _HostedAdapterWithoutTerminalHead:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self) -> None:
        self.worktree_paths: list[object] = []

    async def run(self, **kwargs: object) -> AgentRunResult:
        self.worktree_paths.append(kwargs.get("worktree_path"))
        return AgentRunResult(returncode=0, stdout="done", stderr="", terminal_head_sha=None)


class _HostedAdapterWithTerminalHead:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self, terminal_head_sha: str) -> None:
        self.terminal_head_sha = terminal_head_sha
        self.hosted_pr_identities: list[dict[str, object] | None] = []
        self.calls: list[dict[str, object]] = []
        self.worktree_paths: list[object] = []

    async def run(self, **kwargs: object) -> AgentRunResult:
        self.calls.append(dict(kwargs))
        hosted_pr_identity = kwargs.get("hosted_pr_identity")
        identity = hosted_pr_identity if isinstance(hosted_pr_identity, dict) else None
        self.hosted_pr_identities.append(identity)
        self.worktree_paths.append(kwargs.get("worktree_path"))
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: remote repair",
            stderr="",
            terminal_head_sha=self.terminal_head_sha,
        )


class _HostedAdapterRaisesWithTerminalHead:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self, terminal_head_sha: str) -> None:
        self.terminal_head_sha = terminal_head_sha
        self.hosted_pr_identities: list[dict[str, object] | None] = []

    async def run(self, **kwargs: object) -> AgentRunResult:
        hosted_pr_identity = kwargs.get("hosted_pr_identity")
        identity = hosted_pr_identity if isinstance(hosted_pr_identity, dict) else None
        self.hosted_pr_identities.append(identity)
        raise AgentRunError(
            agent=AgentRuntime.codex,
            result=CommandResult(returncode=1, stdout="pushed", stderr="failed"),
            reason_code="AGENT_CLI_FAILED",
            details={"terminal_head_sha": self.terminal_head_sha},
        )


class _HostedAdapterRetriesAfterTerminalHead:
    name = AgentRuntime.codex
    is_hosted = True

    def __init__(self, terminal_head_sha: str) -> None:
        self.terminal_head_sha = terminal_head_sha
        self.hosted_pr_identities: list[dict[str, object] | None] = []
        self.git_preparations: list[object] = []

    async def run(self, **kwargs: object) -> AgentRunResult:
        self.git_preparations.append(kwargs.get("git_preparation"))
        hosted_pr_identity = kwargs.get("hosted_pr_identity")
        identity = hosted_pr_identity if isinstance(hosted_pr_identity, dict) else None
        self.hosted_pr_identities.append(identity)
        if len(self.hosted_pr_identities) == 1:
            raise AgentRunError(
                agent=AgentRuntime.codex,
                result=CommandResult(returncode=1, stdout="pushed", stderr="timeout"),
                reason_code="AGENT_TIMEOUT",
                details={"terminal_head_sha": self.terminal_head_sha},
            )
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


def _monitor_context_with_adapter(
    adapter: object,
    *,
    worktrees_root: Path | None = None,
) -> SimpleNamespace:
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
        # Hosted agent-start resolves env_file relative to the worktree.
        _worktrees_root=worktrees_root if worktrees_root is not None else Path("/tmp"),
    )


@pytest.mark.unit
async def test_sync_hosted_worktree_fetches_and_resets_terminal_head(tmp_path: Path) -> None:
    sha = "b" * 40
    runner = _Runner(fetched_sha=sha)
    worktree_path = tmp_path / "ws_hosted"

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
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
        ],
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            "--no-tags",
            "git@github.com:dimileeh/aira-web.git",
            "feature/ready",
        ],
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "FETCH_HEAD",
        ],
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "reset",
            "--hard",
            sha,
        ],
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "diff",
            "--name-status",
            "-z",
            f"{'a' * 40}..{sha}",
            "--",
        ],
    ]


@pytest.mark.unit
async def test_sync_hosted_worktree_requires_remote_pr_head_identity(tmp_path: Path) -> None:
    runner = _Runner(fetched_sha="b" * 40)

    with pytest.raises(AgentRunError) as excinfo:
        await _sync_hosted_worktree_to_terminal_head(
            _runner_context(tmp_path, runner),
            workspace_id="ws_hosted",
            hosted_pr_identity={},
            terminal_head_sha="b" * 40,
        )

    assert excinfo.value.reason_code == "HOSTED_REMOTE_HEAD_IDENTITY_MISSING"
    assert excinfo.value.result.stderr == "hosted repair missing remote PR head identity"
    assert runner.calls == []


@pytest.mark.unit
async def test_sync_hosted_worktree_fails_closed_when_current_head_is_unavailable(
    tmp_path: Path,
) -> None:
    runner = _Runner(fetched_sha="b" * 40, current_sha="")
    worktree_path = tmp_path / "ws_hosted"

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

    assert excinfo.value.reason_code == "HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE"
    assert runner.calls == [
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "rev-parse",
            "HEAD",
        ]
    ]


@pytest.mark.unit
async def test_sync_hosted_worktree_fails_closed_when_pr_head_fetch_fails(
    tmp_path: Path,
) -> None:
    runner = _Runner(
        fetched_sha="b" * 40,
        fetch_returncode=128,
        fetch_stderr="fatal: could not fetch PR head",
    )
    worktree_path = tmp_path / "ws_hosted"

    with pytest.raises(AgentRunError) as excinfo:
        await _sync_hosted_worktree_to_terminal_head(
            _runner_context(tmp_path, runner),
            workspace_id="ws_hosted",
            hosted_pr_identity={
                "head_repo_url": "git@github.com:dimileeh/aira-web.git",
                "head_ref": "feature/ready",
            },
            terminal_head_sha="b" * 40,
            operation_start_head="a" * 40,
        )

    assert excinfo.value.reason_code == "HOSTED_REMOTE_HEAD_FETCH_FAILED"
    assert excinfo.value.result.stderr == "fatal: could not fetch PR head"
    assert runner.calls == [
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            "--no-tags",
            "git@github.com:dimileeh/aira-web.git",
            "feature/ready",
        ]
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

    assert len(runner.envs) == 5
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
    worktree_path = tmp_path / "ws_hosted"

    await _sync_hosted_worktree_to_terminal_head(
        _runner_context(tmp_path, runner),
        workspace_id="ws_hosted",
        hosted_pr_identity={
            "head_repo_url": "git@github.com:dimileeh/aira-web.git",
            "head_ref": "feature/ready",
        },
        terminal_head_sha=sha.upper(),
    )

    assert runner.calls[-2] == [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
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
async def test_hosted_pr_identity_matches_handoff_and_recovery_precedence() -> None:
    workspace = SimpleNamespace(
        repo_url="git@github.com:dimileeh/aira-web.git",
        pr_url=None,
        pr_number=None,
        branch_base="main",
        remote_push_branch="awf/ws_hosted",
        owned_paths=["src/awf"],
        monitor_last_commit_sha="b" * 40,
        task_policy={
            "pr_adoption": {
                "pr_url": "https://github.com/dimileeh/aira-web/pull/751",
                "pr_number": 751,
                "base_ref": "development",
                "head_ref": "feature/stale-policy",
                "head_repo_url": "git@github.com:dimileeh/aira-web-fork.git",
                "head_repo_slug": "dimileeh/aira-web-fork",
                "head_sha": "a" * 40,
            }
        },
    )

    async def _load_workspace(_workspace_id: str) -> SimpleNamespace:
        return workspace

    context = SimpleNamespace(_load_workspace=_load_workspace)
    expected = {
        "repo_url": "git@github.com:dimileeh/aira-web.git",
        "pr_url": "https://github.com/dimileeh/aira-web/pull/751",
        "pr_number": 751,
        "base_ref": "development",
        "head_ref": "awf/ws_hosted",
        "head_repo_url": "git@github.com:dimileeh/aira-web-fork.git",
        "head_repo_slug": "dimileeh/aira-web-fork",
        "owned_paths": ["src/awf"],
        "expected_head_sha": "b" * 40,
    }

    assert _hosted_handoff_pr_identity(workspace) == expected
    assert await _hosted_pr_identity_for_workspace(context, "ws_hosted") == expected


@pytest.mark.parametrize(
    ("current_head_ref", "expected_head_ref"),
    [
        pytest.param("feature/renamed", "feature/renamed", id="renamed"),
        pytest.param(None, None, id="blank-fails-closed"),
    ],
)
@pytest.mark.unit
async def test_hosted_pr_identity_uses_checked_current_pr_head_ref(
    current_head_ref: str | None,
    expected_head_ref: str | None,
) -> None:
    workspace = SimpleNamespace(
        repo_url="git@github.com:dimileeh/aira-web.git",
        pr_url="https://github.com/dimileeh/aira-web/pull/751",
        pr_number=751,
        branch_base="main",
        remote_push_branch="feature/old-name",
        owned_paths=[],
        monitor_last_commit_sha="a" * 40,
        task_policy={"pr_adoption": {"head_ref": "feature/adoption-old-name"}},
    )

    async def _load_workspace(_workspace_id: str) -> SimpleNamespace:
        return workspace

    state = SimpleNamespace(
        current_pr_head_ref=current_head_ref,
        current_pr_head_ref_checked=True,
        last_push_sha="a" * 40,
    )

    identity = await _hosted_pr_identity_for_workspace(
        SimpleNamespace(_load_workspace=_load_workspace),
        "ws_hosted",
        state=state,
    )

    assert identity["head_ref"] == expected_head_ref


@pytest.mark.unit
async def test_hosted_agent_success_without_terminal_head_fails_closed(
    tmp_path: Path,
) -> None:
    adapter = _HostedAdapterWithoutTerminalHead()
    with pytest.raises(AgentRunError) as excinfo:
        await _run_monitor_agent_with_service_recovery(
            _monitor_context_with_adapter(adapter, worktrees_root=tmp_path),
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=Path("/tmp/missing-compose.yml"),
            prompt="fix review",
            log_source="monitor",
        )

    assert excinfo.value.reason_code == "HOSTED_REMOTE_HEAD_MISSING"
    assert adapter.worktree_paths == [tmp_path / "ws_hosted"]


@pytest.mark.unit
async def test_ordinary_hosted_monitor_run_omits_git_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sha = "b" * 40
    adapter = _HostedAdapterWithTerminalHead(sha)
    context = _monitor_context_with_adapter(adapter, worktrees_root=tmp_path)

    async def _sync(*_args: object, **_kwargs: object) -> str:
        return sha

    monkeypatch.setattr(agent_service_recovery, "_sync_hosted_worktree_to_terminal_head", _sync)

    await _run_monitor_agent_with_service_recovery(
        context,
        workspace_id="ws_hosted",
        compose_project="awf_ws_hosted",
        compose_file=Path("/tmp/missing-compose.yml"),
        prompt="fix ordinary review",
        log_source="monitor.review",
    )

    assert "git_preparation" not in adapter.calls[0]


@pytest.mark.unit
async def test_hosted_monitor_git_preparation_base_ref_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    adapter = _HostedAdapterWithTerminalHead("b" * 40)
    preparation = AgentRuntimeGitPreparation(
        mode="merge_base",
        base_ref="development",
        expected_base_sha="c" * 40,
    )

    with pytest.raises(AgentRunError) as excinfo:
        await _run_monitor_agent_with_service_recovery(
            _monitor_context_with_adapter(adapter, worktrees_root=tmp_path),
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=Path("/tmp/missing-compose.yml"),
            prompt="fix merge conflict",
            log_source="recovery",
            git_preparation=preparation,
        )

    assert excinfo.value.reason_code == "HOSTED_GIT_PREPARATION_BASE_REF_MISMATCH"
    assert adapter.calls == []


@pytest.mark.unit
async def test_record_hosted_terminal_head_sync_requires_forward_descent(
    tmp_path: Path,
) -> None:
    start = "a" * 40
    synced = "b" * 40
    lateral = "c" * 40
    worktree_path = tmp_path / "ws_hosted"
    worktree_path.mkdir()

    async def _descends_forward(
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        return descendant.lower() == synced.lower() and ancestor.lower() == start.lower()

    runner = SimpleNamespace(_head_descends_from=_descends_forward)
    forward_state = MonitorState(last_push_sha=start)
    await agent_service_recovery._record_hosted_terminal_head_sync(
        runner,
        forward_state,
        synced_head_sha=synced,
        operation_start_head=start,
        worktree_path=worktree_path,
    )
    assert forward_state.last_push_sha == synced
    assert forward_state.hosted_terminal_head_advanced is True

    lateral_state = MonitorState(last_push_sha=start)
    await agent_service_recovery._record_hosted_terminal_head_sync(
        runner,
        lateral_state,
        synced_head_sha=lateral,
        operation_start_head=start,
        worktree_path=worktree_path,
    )
    assert lateral_state.last_push_sha == lateral
    assert lateral_state.hosted_terminal_head_advanced is False


@pytest.mark.unit
async def test_record_hosted_terminal_head_sync_fails_closed_without_ancestry_helper(
    tmp_path: Path,
) -> None:
    start = "a" * 40
    synced = "b" * 40
    worktree_path = tmp_path / "ws_hosted"
    worktree_path.mkdir()
    state = MonitorState(last_push_sha=start)

    await agent_service_recovery._record_hosted_terminal_head_sync(
        SimpleNamespace(),
        state,
        synced_head_sha=synced,
        operation_start_head=start,
        worktree_path=worktree_path,
    )

    assert state.last_push_sha == synced
    assert state.hosted_terminal_head_advanced is False


@pytest.mark.unit
async def test_rollback_hosted_terminal_head_on_remote_force_pushes_and_verifies(
    tmp_path: Path,
) -> None:
    start = "a" * 40
    bad = "b" * 40
    worktree_path = tmp_path / "ws_hosted"
    worktree_path.mkdir()

    class _RollbackRunner(_Runner):
        async def run(
            self,
            args: list[str],
            *,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            if "push" in args and "--force-with-lease" in " ".join(args):
                self.calls.append(args)
                return CommandResult(returncode=0, stdout="", stderr="")
            return await super().run(args, env=env)

    runner = _RollbackRunner(fetched_sha=start, current_sha=start)
    context = SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=runner),
    )
    identity = {
        "head_repo_url": "https://example.invalid/awf.git",
        "head_ref": "feature/ready",
        "repo_url": "https://example.invalid/awf.git",
    }

    ok = await _rollback_hosted_terminal_head_on_remote(
        context,
        workspace_id="ws_hosted",
        hosted_pr_identity=identity,
        rollback_target_sha=start,
        expected_remote_head_sha=bad,
    )

    assert ok
    assert any("push" in call and "--force-with-lease" in " ".join(call) for call in runner.calls)


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


@pytest.mark.unit
async def test_hosted_agent_success_includes_result_output_in_terminal_head_gate_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    adapter = _HostedAdapterWithTerminalHead(sha.upper())
    context = _monitor_context_with_adapter(adapter, worktrees_root=tmp_path)
    command_evidence = ["previous evidence"]
    captured_command_evidence: list[tuple[str, ...]] = []

    async def _capture_sync(*_args: object, **kwargs: object) -> str:
        captured_command_evidence.append(tuple(kwargs["command_evidence"]))
        return sha

    monkeypatch.setattr(
        agent_service_recovery,
        "_sync_hosted_worktree_to_terminal_head",
        _capture_sync,
    )

    result = await _run_monitor_agent_with_service_recovery(
        context,
        workspace_id="ws_hosted",
        compose_project="awf_ws_hosted",
        compose_file=Path("/tmp/missing-compose.yml"),
        prompt="fix review",
        log_source="monitor",
        command_evidence=command_evidence,
    )

    assert result.terminal_head_sha == sha.upper()
    assert adapter.worktree_paths == [tmp_path / "ws_hosted"]
    assert captured_command_evidence == [("previous evidence", "AWF-VERDICT: FIXED: remote repair")]
    assert command_evidence == [
        "previous evidence",
        "AWF-VERDICT: FIXED: remote repair",
    ]


@pytest.mark.unit
async def test_hosted_agent_error_syncs_terminal_head_before_reraising(
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    runner = _Runner(fetched_sha=sha)
    adapter = _HostedAdapterRaisesWithTerminalHead(sha.upper())
    state = MonitorState(last_push_sha="a" * 40)
    context = _monitor_context_with_runner(tmp_path, runner=runner, adapter=adapter)
    worktree_path = tmp_path / "ws_hosted"

    with pytest.raises(AgentRunError) as excinfo:
        await _run_monitor_agent_with_service_recovery(
            context,
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=Path("/tmp/missing-compose.yml"),
            prompt="fix review",
            log_source="monitor",
            state=state,
        )

    assert excinfo.value.reason_code == "AGENT_CLI_FAILED"
    assert state.last_push_sha == sha
    assert adapter.hosted_pr_identities[0]["expected_head_sha"] == "a" * 40
    assert runner.calls[-2] == [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        "reset",
        "--hard",
        sha,
    ]


@pytest.mark.unit
async def test_hosted_agent_error_syncs_terminal_head_without_monitor_state(
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    runner = _Runner(fetched_sha=sha)
    adapter = _HostedAdapterRaisesWithTerminalHead(sha.upper())
    context = _monitor_context_with_runner(tmp_path, runner=runner, adapter=adapter)
    worktree_path = tmp_path / "ws_hosted"

    with pytest.raises(AgentRunError) as excinfo:
        await _run_monitor_agent_with_service_recovery(
            context,
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=Path("/tmp/missing-compose.yml"),
            prompt="fix review",
            log_source="monitor",
        )

    assert excinfo.value.reason_code == "AGENT_CLI_FAILED"
    assert adapter.hosted_pr_identities[0]["expected_head_sha"] == "a" * 40
    assert runner.calls[-2] == [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        "reset",
        "--hard",
        sha,
    ]


@pytest.mark.unit
async def test_hosted_agent_retry_refreshes_pr_identity_after_terminal_head_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    runner = _Runner(fetched_sha=sha)
    adapter = _HostedAdapterRetriesAfterTerminalHead(sha.upper())
    state = MonitorState(last_push_sha="a" * 40)
    context = _monitor_context_with_runner(tmp_path, runner=runner, adapter=adapter)

    async def _recover_after_terminal_head_sync(*_args: object, **_kwargs: object) -> int:
        return 1

    async def _skip_pre_launch_guards(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_error",
        _recover_after_terminal_head_sync,
    )
    monkeypatch.setattr(
        agent_service_recovery,
        "_rerun_monitor_agent_pre_launch_guards",
        _skip_pre_launch_guards,
    )

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
    assert adapter.hosted_pr_identities[1]["expected_head_sha"] == sha


@pytest.mark.unit
async def test_hosted_agent_retry_preserves_explicit_git_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    runner = _Runner(fetched_sha=sha)
    adapter = _HostedAdapterRetriesAfterTerminalHead(sha)
    context = _monitor_context_with_runner(tmp_path, runner=runner, adapter=adapter)
    preparation = AgentRuntimeGitPreparation(
        mode="merge_base",
        base_ref="main",
        expected_base_sha="c" * 40,
    )

    async def _recover(*_args: object, **_kwargs: object) -> int:
        return 1

    async def _skip_guards(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        agent_service_recovery, "_recover_monitor_agent_service_after_error", _recover
    )
    monkeypatch.setattr(
        agent_service_recovery, "_rerun_monitor_agent_pre_launch_guards", _skip_guards
    )

    await _run_monitor_agent_with_service_recovery(
        context,
        workspace_id="ws_hosted",
        compose_project="awf_ws_hosted",
        compose_file=Path("/tmp/missing-compose.yml"),
        prompt="fix merge conflict",
        log_source="recovery",
        state=MonitorState(last_push_sha="a" * 40),
        git_preparation=preparation,
    )

    assert adapter.git_preparations == [preparation, preparation]
