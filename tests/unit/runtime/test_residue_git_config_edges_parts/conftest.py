"""Isolate the module-level item-start caches for every test in this directory."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_fingerprint_git_config as gc,
)


@pytest.fixture(autouse=True)
def _isolated_item_start_caches() -> Iterator[None]:
    """Snapshot and restore the module-level item-start caches around each test."""
    saved = (
        dict(gc._ITEM_START_LOCAL_GIT_CONFIGS),
        dict(gc._ITEM_START_GIT_LINKAGE),
        dict(gc._ITEM_START_COMMONDIR),
        dict(gc._ITEM_START_NESTED_GIT_LINKAGES),
    )
    try:
        yield
    finally:
        for cache, snapshot in zip(
            (
                gc._ITEM_START_LOCAL_GIT_CONFIGS,
                gc._ITEM_START_GIT_LINKAGE,
                gc._ITEM_START_COMMONDIR,
                gc._ITEM_START_NESTED_GIT_LINKAGES,
            ),
            saved,
            strict=True,
        ):
            cache.clear()
            cache.update(snapshot)
