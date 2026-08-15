"""Legacy clarification model-network cleanup regression coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import pytest
import yaml

from awf.adapters import base as adapter_base
from awf.adapters.base import AgentRunError
from awf.adapters.opencode import OpenCodeAdapter
from awf.common.commands import CommandResult, FakeCommandRunner
from tests.unit.adapters.test_adapter_legacy_reask import (
    _PROMPT,
    _write_legacy_opencode_ollama_compose,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("network_exists", "container_ids", "expected_calls"),
    [
        (False, "", 4),
        (True, "", 2),
    ],
    ids=["new-network", "existing-network"],
)
async def test_isolated_reask_refuses_to_start_without_a_live_model_sidecar(
    tmp_path: Path,
    network_exists: bool,
    container_ids: str,
    expected_calls: int,
) -> None:
    """A migration never starts clarification against an absent model endpoint."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
    original_compose_file = compose_file.read_bytes()
    runner = FakeCommandRunner()
    if network_exists:
        runner.queue_result()
    else:
        runner.queue_result(
            returncode=1,
            stderr=(
                "Error response from daemon: network "
                "awf-ws_legacy-clarification-model-net not found"
            ),
        )
        runner.queue_result()
    runner.queue_result(stdout=container_ids)
    if not network_exists:
        runner.queue_result()
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
    assert len(runner.calls) == expected_calls
    assert all("run" not in call.args for call in runner.calls)
    assert all("--force-recreate" not in call.args for call in runner.calls)
    assert compose_file.read_bytes() == original_compose_file


@pytest.mark.unit
async def test_isolated_reask_preserves_migration_when_network_cleanup_fails(
    tmp_path: Path,
) -> None:
    """A failed detach is surfaced instead of mutating the model sidecar."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=1,
        stderr="Error response from daemon: network awf-ws_legacy-clarification-model-net not found",
    )
    runner.queue_result()
    runner.queue_result(stdout="stateful-model-container\n")
    runner.queue_result(returncode=1, stderr="could not attach model")
    runner.queue_result(returncode=1, stderr="network endpoint remains attached")
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

    assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
    assert exc.value.result.stderr == "network endpoint remains attached"
    rendered = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert rendered["services"]["ollama-sidecar"]["networks"] == [
        "awf_net",
        "clarification_model_net",
    ]
    assert all("--force-recreate" not in call.args for call in runner.calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rollback_result", "expected_stderr"),
    [
        (
            CommandResult(returncode=1, stdout="", stderr="network endpoint remains attached"),
            "network endpoint remains attached",
        ),
        (RuntimeError("network rollback crashed"), "RuntimeError: network rollback crashed"),
    ],
    ids=["failed-result", "raised-error"],
)
async def test_isolated_reask_surfaces_cleanup_failure_after_attachment_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rollback_result: CommandResult | RuntimeError,
    expected_stderr: str,
) -> None:
    """Attachment errors never conceal their failed network cleanup."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

    async def _raise_attachment_error(*args: Any, **kwargs: Any) -> Any:
        attachment = kwargs["attachment"]
        attachment.created_network = True
        attachment.connected_container_ids.append("stateful-model-container")
        raise RuntimeError("network attach crashed")

    async def _fail_rollback(*args: Any, **kwargs: Any) -> CommandResult:
        if isinstance(rollback_result, Exception):
            raise rollback_result
        return rollback_result

    monkeypatch.setattr(
        adapter_base,
        "_attach_persisted_clarification_model_network",
        _raise_attachment_error,
    )
    monkeypatch.setattr(
        adapter_base,
        "_rollback_persisted_clarification_model_network",
        _fail_rollback,
    )
    adapter = OpenCodeAdapter(runner=FakeCommandRunner())

    with pytest.raises(AgentRunError) as exc:
        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

    assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
    assert exc.value.result.stderr == expected_stderr


@pytest.mark.unit
async def test_isolated_reask_surfaces_compose_restore_failure_after_attachment_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Attachment errors never conceal a failed Compose-file restoration."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

    async def _raise_attachment_error(*args: Any, **kwargs: Any) -> Any:
        attachment = kwargs["attachment"]
        attachment.created_network = True
        attachment.connected_container_ids.append("stateful-model-container")
        raise RuntimeError("network attach crashed")

    async def _successful_rollback(*args: Any, **kwargs: Any) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    def _fail_restore(*args: Any, **kwargs: Any) -> None:
        raise OSError("compose storage unavailable")

    monkeypatch.setattr(
        adapter_base,
        "_attach_persisted_clarification_model_network",
        _raise_attachment_error,
    )
    monkeypatch.setattr(
        adapter_base,
        "_rollback_persisted_clarification_model_network",
        _successful_rollback,
    )
    monkeypatch.setattr(adapter_base, "_restore_compose_file", _fail_restore)
    adapter = OpenCodeAdapter(runner=FakeCommandRunner())

    with pytest.raises(AgentRunError) as exc:
        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

    assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
    assert exc.value.result.stderr == "OSError: compose storage unavailable"


@pytest.mark.unit
async def test_isolated_reask_surfaces_compose_restore_failure_after_failed_service_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed update never conceals a failed Compose-file restoration."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)

    async def _failed_service_update(*args: Any, **kwargs: Any) -> Any:
        return kwargs["attachment"], CommandResult(
            returncode=1, stdout="", stderr="model service update failed"
        )

    async def _successful_rollback(*args: Any, **kwargs: Any) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    def _fail_restore(*args: Any, **kwargs: Any) -> None:
        raise OSError("compose storage unavailable")

    monkeypatch.setattr(
        adapter_base,
        "_attach_persisted_clarification_model_network",
        _failed_service_update,
    )
    monkeypatch.setattr(
        adapter_base,
        "_rollback_persisted_clarification_model_network",
        _successful_rollback,
    )
    monkeypatch.setattr(adapter_base, "_restore_compose_file", _fail_restore)
    adapter = OpenCodeAdapter(runner=FakeCommandRunner())

    with pytest.raises(AgentRunError) as exc:
        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )

    assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
    assert exc.value.result.stderr == "OSError: compose storage unavailable"


@pytest.mark.unit
async def test_isolated_reask_surfaces_compose_restore_failure_during_upgrade_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancellation cannot hide a failed pre-attachment Compose restore."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
    upgrade_started = Event()
    release_upgrade = Event()

    def _upgrade_after_cancellation(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        upgrade_started.set()
        assert release_upgrade.wait(timeout=1)
        compose_file.write_bytes(b"upgraded compose")
        return ()

    def _fail_restore(*args: Any, **kwargs: Any) -> None:
        raise OSError("compose storage unavailable")

    monkeypatch.setattr(
        adapter_base,
        "upgrade_persisted_clarification_service",
        _upgrade_after_cancellation,
    )
    monkeypatch.setattr(adapter_base, "_restore_compose_file", _fail_restore)
    adapter = OpenCodeAdapter(runner=FakeCommandRunner())
    run_task = asyncio.create_task(
        adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )
    )

    await asyncio.wait_for(asyncio.to_thread(upgrade_started.wait), timeout=0.2)
    run_task.cancel()
    release_upgrade.set()

    with pytest.raises(AgentRunError) as exc:
        await run_task

    assert exc.value.reason_code == "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
    assert exc.value.result.stderr == "OSError: compose storage unavailable"
    assert compose_file.read_bytes() == b"upgraded compose"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attachment_outcome", "cancellation_stage"),
    [
        ("error", "rollback"),
        ("error", "restore"),
        ("failed-update", "rollback"),
        ("failed-update", "restore"),
    ],
)
async def test_isolated_reask_cancellation_waits_for_migration_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attachment_outcome: str,
    cancellation_stage: str,
) -> None:
    """Cancellation waits for migration rollback and Compose restoration."""
    compose_file = _write_legacy_opencode_ollama_compose(tmp_path)
    original_compose_file = compose_file.read_bytes()
    rollback_started = asyncio.Event()
    allow_rollback = asyncio.Event()
    rollback_finished = asyncio.Event()
    restore_started = Event()
    allow_restore = Event()
    restore_finished = Event()

    async def _attachment_that_needs_cleanup(*args: Any, **kwargs: Any) -> Any:
        attachment = kwargs["attachment"]
        attachment.created_network = True
        attachment.connected_container_ids.append("stateful-model-container")
        compose_file.write_bytes(b"partially migrated compose")
        if attachment_outcome == "error":
            raise RuntimeError("network attach crashed")
        return attachment, CommandResult(returncode=1, stdout="", stderr="service update failed")

    async def _blocking_rollback(*args: Any, **kwargs: Any) -> CommandResult:
        rollback_started.set()
        if cancellation_stage == "rollback":
            await allow_rollback.wait()
        rollback_finished.set()
        return CommandResult(returncode=0, stdout="", stderr="")

    def _blocking_restore(*args: Any, **kwargs: Any) -> None:
        restore_started.set()
        if cancellation_stage == "restore":
            assert allow_restore.wait(timeout=1)
        compose_file.write_bytes(kwargs["contents"])
        restore_finished.set()

    monkeypatch.setattr(
        adapter_base,
        "_attach_persisted_clarification_model_network",
        _attachment_that_needs_cleanup,
    )
    monkeypatch.setattr(
        adapter_base,
        "_rollback_persisted_clarification_model_network",
        _blocking_rollback,
    )
    monkeypatch.setattr(adapter_base, "_restore_compose_file", _blocking_restore)
    adapter = OpenCodeAdapter(runner=FakeCommandRunner())
    run_task = asyncio.create_task(
        adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt=_PROMPT,
            model="ollama/kimi-k2.6:cloud",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=tmp_path / "reask",
        )
    )

    if cancellation_stage == "rollback":
        await asyncio.wait_for(rollback_started.wait(), timeout=0.2)
    else:
        await asyncio.wait_for(asyncio.to_thread(restore_started.wait), timeout=0.2)
    run_task.cancel()
    await asyncio.sleep(0)

    assert not run_task.done()

    if cancellation_stage == "rollback":
        allow_rollback.set()
        await asyncio.wait_for(rollback_finished.wait(), timeout=0.2)
    else:
        allow_restore.set()
        await asyncio.wait_for(asyncio.to_thread(restore_finished.wait), timeout=0.2)
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert compose_file.read_bytes() == original_compose_file
