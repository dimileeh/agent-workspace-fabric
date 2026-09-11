"""Fixtures shared by the worktree activity probe part modules."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.unit.adapters.test_worktree_activity_probe_parts.helpers import _age_tree


@pytest.fixture(autouse=True)
def settle_scan_threads(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    # Depend on monkeypatch so scan callbacks finish before patched counters and
    # methods are restored. Awaiting a probe result need not finish its thread.
    def settle() -> None:
        deadline = time.monotonic() + 10.0
        for thread in threading.enumerate():
            if thread.name.startswith("awf-worktree-scan"):
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                assert not thread.is_alive(), f"test left scan thread running: {thread.name}"

    yield settle
    settle()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "ws_probe"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "src" / "nested" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _age_tree(root)
    return root
