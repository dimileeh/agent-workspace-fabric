"""Companion image prune wiring in terminal workspace GC (issue #298)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.session import make_session_factory
from awf.service.gc import run_terminal_workspace_gc


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an async session factory bound to the test engine."""
    yield make_session_factory(engine)


@pytest.mark.unit
async def test_execute_invokes_companion_image_prune_and_reports_result(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """GC invokes the companion image prune callback and reports its result."""
    calls: list[bool] = []

    async def _prune() -> dict[str, object]:
        calls.append(True)
        return {
            "status": "succeeded",
            "reason_code": "COMPANION_IMAGE_PRUNE_SUCCEEDED",
            "output": "Total reclaimed space: 1GB",
        }

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        execute=True,
        companion_image_prune=_prune,
    )

    assert calls == [True]
    payload = result.to_dict()
    assert payload["companion_image_prune"] == {
        "status": "succeeded",
        "reason_code": "COMPANION_IMAGE_PRUNE_SUCCEEDED",
        "output": "Total reclaimed space: 1GB",
    }


@pytest.mark.unit
async def test_execute_without_prune_callback_omits_report_key(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """GC omits the prune report key when no prune callback is provided."""
    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        execute=True,
    )

    assert "companion_image_prune" not in result.to_dict()


@pytest.mark.unit
async def test_dry_run_does_not_invoke_companion_image_prune(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dry-run GC does not invoke the companion image prune callback."""
    calls: list[bool] = []

    async def _prune() -> dict[str, object]:
        calls.append(True)
        return {}

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        execute=False,
        companion_image_prune=_prune,
    )

    assert calls == []
    assert "companion_image_prune" not in result.to_dict()
