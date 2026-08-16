"""Compatibility import target for the split health route tests.

The AWF targeted validation command still addresses the historical route-level
path. Keep this shim non-collectable so default discovery collects the canonical
split tests only once.
"""

__test__ = False

from tests.unit.api.test_health_parts.test_health_part_001 import *  # noqa: F403
