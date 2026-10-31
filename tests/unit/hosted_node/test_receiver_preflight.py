"""Recovery material preflight: striped file checks at the same bounded concurrency."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.schemas import Identity


class Store:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.checked = []
        self.in_flight = self.peak = 0
        self.lock = threading.Lock()

    def exists(self, directory, slot):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.checked.append((directory, slot))
        try:
            return (directory, slot) not in self.missing
        finally:
            with self.lock:
                self.in_flight -= 1


def preflight(monkeypatch, count, *, store=None, rechecked=None):
    config = HostedSettings(
        owner="owner",
        incarnation="vm",
        recovery_generation=1,
        controller_token="controller-test-only",
        material_root=Path("/unused-no-material-access"),
    )
    identity = Identity(owner="owner", incarnation="vm", recovery_generation=1, boot_epoch=1)
    receiver = Receiver(config, Mock(), identity, store or Store())
    slots = [(f"ws-{index}", f"slot-{index}") for index in range(count)]
    lookups = []
    hops = []

    async def material_slots(bindings):
        lookups.append(bindings)
        if rechecked is not None and len(lookups) > 1:
            return rechecked
        return list(slots)

    original = asyncio.to_thread

    async def to_thread(function, *args):
        hops.append(args)
        return await original(function, *args)

    monkeypatch.setattr(receiver, "_recovery_material_slots", material_slots)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    return receiver, slots, lookups, hops


@pytest.mark.parametrize("count,expected_hops", [(1024, 8), (20, 8), (3, 3)])
async def test_preflight_checks_every_slot_once_in_at_most_eight_hops(
    monkeypatch, count, expected_hops
):
    store = Store()
    receiver, slots, lookups, hops = preflight(monkeypatch, count, store=store)
    assert await receiver.verify_recovery_bindings([]) is True
    # One thread hop per stripe, not one per slot; the stripes partition the cohort.
    assert len(hops) == expected_hops
    assert sorted(slot for (stripe,) in hops for slot in stripe) == sorted(slots)
    assert sorted(store.checked) == sorted(slots) and store.peak <= 8
    assert len(lookups) == 2


async def test_empty_preflight_skips_file_io_and_still_rechecks_fences(monkeypatch):
    receiver, _, lookups, hops = preflight(monkeypatch, 0)
    assert await receiver.verify_recovery_bindings([]) is True
    assert hops == [] and len(lookups) == 2


async def test_missing_material_denies_before_the_fence_recheck(monkeypatch):
    store = Store(missing={("ws-13", "slot-13")})
    receiver, _, lookups, _ = preflight(monkeypatch, 20, store=store)
    assert await receiver.verify_recovery_bindings([]) is False
    assert len(lookups) == 1


async def test_fence_moved_during_file_io_dominates(monkeypatch):
    receiver, _, lookups, _ = preflight(monkeypatch, 20, rechecked=[("ws-0", "slot-0")])
    assert await receiver.verify_recovery_bindings([]) is False
    assert len(lookups) == 2


async def test_unresolvable_bindings_never_touch_material(monkeypatch):
    store = Store()
    receiver, _, _, hops = preflight(monkeypatch, 20, store=store)

    async def unresolved(bindings):
        return None

    monkeypatch.setattr(receiver, "_recovery_material_slots", unresolved)
    assert await receiver.verify_recovery_bindings([]) is False
    assert hops == [] and store.checked == []
