"""Shared fixtures for ``awf.node`` unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from awf.node.auth_mounts_overlay_probe import reset_overlay_probe_cache


@pytest.fixture(autouse=True)
def _reset_overlay_probe_cache() -> Iterator[None]:
    """Keep the module-level overlay-probe cache from leaking between tests.

    :func:`awf.node.auth_mounts_overlay_probe.cached_overlay_probe` memoizes per
    ``scratch_root`` for the life of the process. Two tests using the same
    ``tmp_path``-derived root — or a test asserting the probe is *not* invoked —
    would otherwise silently observe a previous test's result, which is the most
    likely source of flaky or unreachable branches here.
    """

    reset_overlay_probe_cache()
    yield
    reset_overlay_probe_cache()
