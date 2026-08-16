"""Cancellation cleanup contracts for isolated and hosted adapter workers."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from awf.adapters import base as adapter_base
from awf.adapters import base_isolated_reask


@pytest.mark.unit
def test_isolated_reask_skips_git_binds_for_a_non_worktree(tmp_path: Path) -> None:
    """A plain directory cannot expose Git metadata to a clarification container."""
    temporary_metadata, binds = adapter_base._isolated_reask_git_metadata_volume_binds(
        tmp_path / "not-a-worktree",
        expected_ref="a" * 40,
        expected_source_mirror=tmp_path / "source-mirror",
    )

    assert temporary_metadata is None
    assert binds == ()


@pytest.mark.unit
async def test_discarded_reask_metadata_task_removes_late_snapshot(tmp_path: Path) -> None:
    """Late snapshot results are cleaned up after cancellation has been propagated."""
    temporary_metadata = tempfile.TemporaryDirectory[str](dir=tmp_path)

    async def _snapshot() -> tuple[
        tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]
    ]:
        return temporary_metadata, ()

    task = asyncio.create_task(_snapshot())
    await task

    base_isolated_reask._discard_isolated_reask_git_metadata_task_result(task)  # noqa: SLF001

    assert not Path(temporary_metadata.name).exists()


@pytest.mark.unit
async def test_discarded_reask_metadata_task_consumes_cancelled_worker() -> None:
    """Cleanup callbacks must not re-raise a worker cancellation."""
    pending = asyncio.Event()

    async def _snapshot() -> tuple[
        tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]
    ]:
        await pending.wait()
        raise AssertionError("cancelled task must not complete")

    task = asyncio.create_task(_snapshot())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    base_isolated_reask._discard_isolated_reask_git_metadata_task_result(task)  # noqa: SLF001


@pytest.mark.unit
async def test_discarded_reask_metadata_task_consumes_worker_error() -> None:
    """A failed off-thread snapshot is consumed without masking the original cancellation."""

    async def _snapshot() -> tuple[
        tempfile.TemporaryDirectory[str] | None, tuple[tuple[Path, str], ...]
    ]:
        raise OSError("snapshot failed")

    task = asyncio.create_task(_snapshot())
    with pytest.raises(OSError, match="snapshot failed"):
        await task

    base_isolated_reask._discard_isolated_reask_git_metadata_task_result(task)  # noqa: SLF001


@pytest.mark.unit
async def test_discarded_hosted_execution_task_consumes_late_worker_error() -> None:
    """A completed hosted worker error cannot leak from a cancellation callback."""

    async def _execute() -> None:
        raise OSError("hosted execution failed")

    task = asyncio.create_task(_execute())
    with pytest.raises(OSError, match="hosted execution failed"):
        await task

    base_isolated_reask._discard_hosted_execute_task_result(task)  # noqa: SLF001


@pytest.mark.unit
async def test_discarded_hosted_execution_task_consumes_worker_cancellation() -> None:
    """A cancellation callback preserves cancellation without re-raising the worker's state."""
    pending = asyncio.Event()

    async def _execute() -> None:
        await pending.wait()

    task = asyncio.create_task(_execute())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    base_isolated_reask._discard_hosted_execute_task_result(task)  # noqa: SLF001


@pytest.mark.unit
async def test_isolated_reask_cancellation_when_git_metadata_task_already_done(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation when git_metadata_task is already done discards result directly."""
    import yaml

    discarded_tasks: list[asyncio.Task[Any]] = []
    original_discard = base_isolated_reask._discard_isolated_reask_git_metadata_task_result

    def _record_discard(task: asyncio.Task[Any]) -> None:
        discarded_tasks.append(task)
        original_discard(task)

    monkeypatch.setattr(
        adapter_base,
        "_discard_isolated_reask_git_metadata_task_result",
        _record_discard,
    )

    call_count = 0

    async def _cancelled_shield(arg: Any) -> Any:
        nonlocal call_count
        call_count += 1
        res = await arg
        if call_count >= 2:
            raise asyncio.CancelledError()
        return res

    monkeypatch.setattr(adapter_base.asyncio, "shield", _cancelled_shield)

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "awf-agent-runtime:latest",
                        "volumes": [f"{tmp_path / 'worktree'}:/workspace"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    from awf.adapters import get_adapter
    from awf.common.commands import FakeCommandRunner

    runner = FakeCommandRunner()
    adapter = get_adapter("opencode", runner=runner)

    reask_path = tmp_path / "reask"
    reask_path.mkdir()

    with pytest.raises(asyncio.CancelledError):
        await adapter.run(
            compose_project="awf_ws_legacy",
            compose_file=compose_file,
            prompt="hello",
            workspace_id="ws_legacy",
            isolated_worktree_host_path=reask_path,
            isolated_worktree_ref="a" * 40,
            isolated_worktree_source_mirror=tmp_path / "source-mirror",
        )

    assert len(discarded_tasks) == 1
    assert discarded_tasks[0].done()
