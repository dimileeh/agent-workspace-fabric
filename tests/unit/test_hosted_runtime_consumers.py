"""Credential and cancellation boundaries around the normal command runner."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.hosted_node.identity import MaterialDeniedError, WorkspaceScopeDeniedError
from awf.hosted_node.runtime_authority import AuthorizedMaterials
from awf.hosted_node.runtime_consumers import HostedCommandRunner, WorkspaceConsumers


def consumers(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "host-token-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "host-agent-must-not-leak")
    authority = AsyncMock()
    authority.workspace.return_value = AuthorizedMaterials(
        {
            "GH_TOKEN": "scoped-github",
            "GITHUB_TOKEN": "scoped-github",
            "CODEX_API_KEY": "scoped-agent",
        },
        datetime.now(UTC) + timedelta(minutes=1),
    )
    scope = WorkspaceConsumers(authority, tmp_path)
    return scope, authority


async def test_scoped_commands_do_not_inherit_host_credentials_or_put_values_in_args(
    tmp_path, monkeypatch
):
    scope, authority = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    runner = HostedCommandRunner(scope, delegate)
    async with scope.scope("ws_exact"):
        await runner.run(["git", "fetch", "origin"])
        await runner.run_streaming(
            ["docker", "compose", "-p", "awf-ws_exact", "exec", "-T", "agent", "codex", "exec"]
        )
    assert authority.workspace.await_count >= 3
    assert all(call.env["GH_TOKEN"] == "scoped-github" for call in delegate.calls)
    assert "host-token" not in repr(delegate.calls)
    assert all("scoped-github" not in repr(call.args) for call in delegate.calls)
    assert "-e" in delegate.calls[1].args
    assert "CODEX_API_KEY" in delegate.calls[1].args
    assert "scoped-agent" not in repr(delegate.calls[1].args)


async def test_revoked_material_denies_next_consumer_before_delegate(tmp_path, monkeypatch):
    scope, authority = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    runner = HostedCommandRunner(scope, delegate)
    scope.stop_workspace = AsyncMock()
    with pytest.raises(WorkspaceScopeDeniedError, match="GRANT_REVOKED"):
        async with scope.scope("ws_exact"):
            authority.workspace.side_effect = MaterialDeniedError("GRANT_REVOKED")
            with pytest.raises(MaterialDeniedError):
                await runner.run(["gh", "pr", "create"])
    assert not delegate.calls
    scope.stop_workspace.assert_awaited_once_with("ws_exact")


async def test_binary_git_output_is_not_decoded_and_secrets_stay_redacted(tmp_path, monkeypatch):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = AsyncMock()
    delegate.run.return_value = CommandResult(
        returncode=0,
        stdout="",
        stderr="",
        stdout_bytes=b"\xff\x00scoped-github\x80",
    )
    async with scope.scope("ws_exact"):
        result = await HostedCommandRunner(scope, delegate).run(
            ["git", "cat-file", "blob", "HEAD:file"]
        )
    assert result.stdout_bytes == b"\xff\x00<redacted>\x80"


async def test_no_scope_cannot_run_forge_or_agent(tmp_path, monkeypatch):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    runner = HostedCommandRunner(scope, delegate)
    with pytest.raises(MaterialDeniedError, match="WORKSPACE_SCOPE_REQUIRED"):
        await runner.run(["gh", "pr", "create"])
    assert not delegate.calls


async def test_expired_running_consumer_is_cancelled_and_exact_runtime_stopped(
    tmp_path, monkeypatch
):
    scope, authority = consumers(tmp_path, monkeypatch)
    stopped = AsyncMock()
    scope.stop_workspace = stopped
    scope.poll_seconds = 0.01
    with pytest.raises(MaterialDeniedError):
        async with scope.scope("ws_exact"):
            authority.workspace.side_effect = MaterialDeniedError("GRANT_REVOKED")
            await asyncio.sleep(10)
    stopped.assert_awaited_once_with("ws_exact")


async def test_worker_shutdown_cancels_only_its_scoped_runtime(tmp_path, monkeypatch):
    scope, _ = consumers(tmp_path, monkeypatch)
    scope.stop_workspace = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        async with scope.scope("ws_exact"):
            raise asyncio.CancelledError()
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)


@pytest.mark.parametrize(
    "error", [MaterialDeniedError("GRANT_REVOKED"), OSError("offline"), TimeoutError()]
)
async def test_caller_cancellation_with_final_authority_failure_is_not_graceful(
    tmp_path, monkeypatch, error
):
    scope, authority = consumers(tmp_path, monkeypatch)
    scope.stop_workspace = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        async with scope.scope("ws_exact"):
            authority.workspace.side_effect = error
            raise asyncio.CancelledError()
    scope.stop_workspace.assert_awaited_once_with("ws_exact")
    authority.retract.assert_awaited_once_with("ws_exact")


async def test_authorized_cancel_preserves_primary_timeout_and_cleanup_failure(
    tmp_path, monkeypatch
):
    scope, authority = consumers(tmp_path, monkeypatch)
    scope.stop_workspace = AsyncMock()
    cancellation = asyncio.CancelledError()
    cancellation.command_reason_code = "AGENT_TIMEOUT"
    with pytest.raises(asyncio.CancelledError) as caught:
        async with scope.scope("ws_exact"):
            raise cancellation
    assert caught.value is cancellation
    assert caught.value.command_reason_code == "AGENT_TIMEOUT"
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)
    authority.retract_publication.assert_awaited_once_with(
        "ws_exact", authority.publication.return_value
    )

    scope.stop_workspace.side_effect = OSError("synthetic cleanup failure")
    with pytest.raises(OSError, match="synthetic cleanup failure"):
        async with scope.scope("ws_exact"):
            raise asyncio.CancelledError()
    authority.retract.assert_awaited_once_with("ws_exact")
    assert authority.retract.await_args.kwargs == {}


async def test_agent_git_trust_is_only_the_managed_container_checkout(tmp_path, monkeypatch):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    runner = HostedCommandRunner(scope, delegate)
    async with scope.scope("ws_exact"):
        await runner.run(["git", "status"])
        await runner.run(
            ["docker", "compose", "exec", "-T", "-w", "/workspace", "agent", "git", "status"]
        )
    host, agent = delegate.calls
    assert "safe.directory" not in host.env.values()
    assert agent.env["GIT_CONFIG_COUNT"] == "3"
    assert agent.env["GIT_CONFIG_KEY_2"] == "safe.directory"
    assert agent.env["GIT_CONFIG_VALUE_2"] == "/workspace"
    assert "GIT_CONFIG_KEY_2" in agent.args
    assert "*" not in agent.env.values()


@pytest.mark.parametrize("service", ["postgres", "agent-helper"])
async def test_companion_exec_never_receives_agent_or_forge_credentials(
    tmp_path, monkeypatch, service
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    async with scope.scope("ws_exact"):
        await HostedCommandRunner(scope, delegate).run(
            ["docker", "compose", "exec", "-T", service, "echo", "agent"]
        )
    call = delegate.calls[0]
    assert "scoped-github" not in call.env.values()
    assert "scoped-agent" not in call.env.values()
    assert "CODEX_API_KEY" not in call.args
    assert "awf-scoped-exec" not in call.args
