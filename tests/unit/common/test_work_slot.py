"""Dependency-free work-slot injection helpers (slot-release)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from awf.common.work_slot import hold_work_slot, work_slot_kwargs

pytestmark = pytest.mark.unit


@dataclass
class _Handle:
    invocation_id: str


@dataclass
class _FakeWorkSlot:
    events: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def hold(self, phase: str) -> AsyncIterator[_Handle]:
        self.events.append(f"enter:{phase}")
        try:
            yield _Handle("wsl_fake")
        finally:
            self.events.append(f"exit:{phase}")

    async def release_idle(self) -> None:
        self.events.append("release_idle")


def test_work_slot_kwargs_only_passes_a_bound_slot() -> None:
    slot = _FakeWorkSlot()
    assert work_slot_kwargs(None) == {}
    assert work_slot_kwargs(slot) == {"work_slot": slot}


async def test_hold_work_slot_without_a_slot_yields_no_marker() -> None:
    async with hold_work_slot(None, "agent") as invocation_id:
        assert invocation_id is None


async def test_hold_work_slot_wraps_the_body_and_yields_the_marker() -> None:
    slot = _FakeWorkSlot()
    async with hold_work_slot(slot, "validation") as invocation_id:
        slot.events.append("body")
        assert invocation_id == "wsl_fake"
    assert slot.events == ["enter:validation", "body", "exit:validation"]
