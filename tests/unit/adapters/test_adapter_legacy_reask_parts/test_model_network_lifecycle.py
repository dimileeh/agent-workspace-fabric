"""Legacy clarification model-network lifecycle regression coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from awf.adapters import base as adapter_base
from awf.adapters.base import AgentRunError
from awf.adapters.opencode import OpenCodeAdapter
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime
from tests.unit.adapters.test_adapter_legacy_reask import (
    _PROMPT,
    _write_legacy_opencode_ollama_compose,
)


class TestLegacyClarificationModelNetworkLifecycle:
    """Legacy model-network lifecycle behavior remains isolated from the primary stack."""

    @pytest.mark.unit
    async def test_isolated_reask_reconciles_interrupted_legacy_model_migration_once(
        self, tmp_path: Path
    ) -> None:
        """A retry attaches an existing sidecar before recording reconciliation."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        assert adapter_base.upgrade_persisted_clarification_service(
            compose_file=compose_file,
            workspace_id="ws_legacy",
            agent_runtime=AgentRuntime.opencode,
            agent_model="ollama/kimi-k2.6:cloud",
        ) == ("ollama-sidecar",)

        runner = FakeCommandRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert runner.calls[2].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert (
            yaml.safe_load(compose_file.read_text(encoding="utf-8"))[
                "x-awf-persisted-clarification-model-network-reconciled"
            ]
            is True
        )

        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

        assert len(runner.calls) == 5
        assert "run" in runner.calls[-1].args

    @pytest.mark.unit
    async def test_isolated_reask_retries_existing_network_after_marker_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed marker retries with a service alias without replacing the sidecar."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        def fail_marker(*, compose_file: Path) -> None:
            del compose_file
            raise ValueError("disk full")

        monkeypatch.setattr(
            adapter_base,
            "mark_persisted_clarification_model_network_reconciled",
            fail_marker,
        )
        runner = FakeCommandRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        adapter = OpenCodeAdapter(runner=runner)

        for _ in range(2):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert "run" in runner.calls[3].args
        assert "run" in runner.calls[-1].args
        assert runner.calls[6].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[7].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[8].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert (
            yaml.safe_load(compose_file.read_text(encoding="utf-8"))[
                "x-awf-persisted-clarification-model-network-reconciled"
            ]
            is False
        )

    @pytest.mark.unit
    async def test_isolated_reask_restores_existing_endpoint_after_alias_reconciliation_fails(
        self, tmp_path: Path
    ) -> None:
        """A failed alias reattach never leaves the stateful sidecar disconnected."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()
        runner = FakeCommandRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="could not attach service alias")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED"
        assert runner.calls[3].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[4].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[5].args == runner.calls[4].args
        assert all("run" not in call.args for call in runner.calls)
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_restores_existing_endpoint_alias(
        self, tmp_path: Path
    ) -> None:
        """Cancellation while detaching an existing endpoint restores its alias."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingDisconnectRunner(FakeCommandRunner):
            """Cancel immediately after Docker can have detached the endpoint."""

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "disconnect"]:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingDisconnectRunner()
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(
            returncode=1,
            stderr="endpoint with name stateful-model-container already exists in network",
        )
        runner.queue_result()
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert runner.calls[4].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_preserves_endpoint_existing_before_connect(
        self, tmp_path: Path
    ) -> None:
        """Cancellation after an uncertain connect keeps a pre-existing endpoint."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingFirstConnectRunner(FakeCommandRunner):
            """Cancel after Docker can have handled the initial connect request."""

            cancelled_connect = False

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "connect"] and not self.cancelled_connect:
                    self.cancelled_connect = True
                    raise asyncio.CancelledError
                return result

        runner = _CancellingFirstConnectRunner()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert runner.calls[3].args == [
            "docker",
            "network",
            "connect",
            "--alias",
            "ollama-sidecar",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert all("disconnect" not in call.args for call in runner.calls)

    @pytest.mark.unit
    async def test_isolated_reask_rolls_back_only_new_network_attachments(
        self, tmp_path: Path
    ) -> None:
        """A failed second attachment leaves stateful model containers untouched."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result(stdout="first-model-container\nsecond-model-container\n")
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="could not attach second model")
        runner.queue_result()
        runner.queue_result(returncode=1, stderr="container is not connected to network")
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert exc.value.reason_code == "CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED"
        assert runner.calls[5].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "second-model-container",
        ]
        assert runner.calls[6].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "first-model-container",
        ]
        assert runner.calls[7].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
        assert all("rm" not in call.args or "compose" not in call.args for call in runner.calls)
        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_detaches_the_existing_sidecar(
        self, tmp_path: Path
    ) -> None:
        """Cancellation after connect never requires model-sidecar recreation."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
        original_compose_file = compose_file.read_bytes()

        class _CancellingConnectRunner(FakeCommandRunner):
            """Cancel immediately after Docker can have applied the connection."""

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "connect"]:
                    raise asyncio.CancelledError
                return result

        runner = _CancellingConnectRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result(stdout="stateful-model-container\n")
        runner.queue_result()
        runner.queue_result()
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert runner.calls[4].args == [
            "docker",
            "network",
            "disconnect",
            "awf-ws_legacy-clarification-model-net",
            "stateful-model-container",
        ]
        assert runner.calls[5].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
        assert all("--force-recreate" not in call.args for call in runner.calls)
        assert compose_file.read_bytes() == original_compose_file

    @pytest.mark.unit
    async def test_isolated_reask_cancellation_removes_network_created_before_runner_returns(
        self, tmp_path: Path
    ) -> None:
        """Cancellation after network creation removes the possibly-created network."""
        compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

        class _CancellingCreateRunner(FakeCommandRunner):
            """Cancel immediately after Docker can have created the network."""

            network_creation_marker: str | None = None

            async def run(self, args: list[str], **kwargs: Any) -> CommandResult:
                result = await super().run(args, **kwargs)
                if args[0:3] == ["docker", "network", "create"]:
                    self.network_creation_marker = next(
                        label.removeprefix("io.awf.clarification-network-creation=")
                        for label in args
                        if label.startswith("io.awf.clarification-network-creation=")
                    )
                    raise asyncio.CancelledError
                if (
                    args[0:3] == ["docker", "network", "inspect"]
                    and self.network_creation_marker is not None
                ):
                    return CommandResult(
                        returncode=result.returncode,
                        stdout=f"{self.network_creation_marker}\n",
                        stderr=result.stderr,
                        reason_code=result.reason_code,
                    )
                return result

        runner = _CancellingCreateRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
        )
        runner.queue_result()
        runner.queue_result()
        adapter = OpenCodeAdapter(runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project="awf_ws_legacy",
                compose_file=compose_file,
                prompt=_PROMPT,
                model="ollama/kimi-k2.6:cloud",
                workspace_id="ws_legacy",
                isolated_worktree_host_path=tmp_path / "reask",
            )

        assert runner.calls[2].args[0:3] == ["docker", "network", "inspect"]
        assert runner.calls[3].args == [
            "docker",
            "network",
            "rm",
            "awf-ws_legacy-clarification-model-net",
        ]
