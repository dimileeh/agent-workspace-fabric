"""Isolated usage-sampling implementation shared by the base adapter seam."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.adapters.usage import IsolatedUsageSampleContext, IsolatedUsageSampler
from awf.common.compose_exec import TrackedIsolatedComposeRun
from awf.common.logging import get_logger

if TYPE_CHECKING:
    from awf.adapters.base import AgentAdapter


_log = get_logger(__name__)


async def complete_isolated_usage_capture_after_cancellation(
    capture: Coroutine[Any, Any, None],
) -> None:
    """Finish a final isolated usage capture despite repeated cancellation."""
    capture_task = asyncio.create_task(capture)
    while not capture_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(capture_task)
    with contextlib.suppress(asyncio.CancelledError):
        capture_task.result()


async def capture_isolated_usage_before_cleanup(
    sampler_ctx: IsolatedUsageSampleContext | None,
    invocation: Any,
    *,
    agent: str,
    workspace_id: str | None,
) -> None:
    """Best-effort final capture before force-removing an isolated container."""
    if sampler_ctx is None or not isinstance(invocation, TrackedIsolatedComposeRun):
        return
    try:
        await sampler_ctx.capture_final_before_cleanup(container_name=invocation.container_name)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "usage.collect.error",
            agent=agent,
            workspace_id=workspace_id,
            phase="capture_before_isolated_cleanup",
            exc_info=True,
        )


async def start_isolated_usage_sampling(
    adapter: AgentAdapter,
    *,
    compose_project: str,
    compose_file: Path,
    workspace_id: str | None,
    cli_args: list[str],
) -> IsolatedUsageSampleContext | None:
    """Start isolated usage capture without abandoning setup on cancellation."""
    sampler = adapter._usage_sampler
    if sampler is None or workspace_id is None or not isinstance(sampler, IsolatedUsageSampler):
        return None
    start_task = asyncio.create_task(
        sampler.start_isolated(
            compose_project=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            provider=adapter.name,
            cli_args=cli_args,
        )
    )
    try:
        return await asyncio.shield(start_task)
    except asyncio.CancelledError:
        # ``start_isolated`` creates its capture directory in a worker thread.
        # Keep the task alive through cancellation so a context returned after
        # that worker finishes can remove the directory.
        while not start_task.done():
            with contextlib.suppress(BaseException):
                await asyncio.shield(start_task)
        try:
            sampler_ctx = start_task.result()
        except (Exception, asyncio.CancelledError):
            pass
        else:
            finalize_task = asyncio.create_task(
                adapter._finalize_usage_sampling(
                    sampler_ctx, status="cancelled", workspace_id=workspace_id
                )
            )
            while not finalize_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(finalize_task)
        raise
    except Exception:
        _log.warning(
            "usage.collect.error",
            agent=adapter.name.value,
            workspace_id=workspace_id,
            phase="start_isolated",
            exc_info=True,
        )
        return None
