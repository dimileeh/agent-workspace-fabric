"""Cancellation cleanup contracts for isolated and hosted adapter workers."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

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
