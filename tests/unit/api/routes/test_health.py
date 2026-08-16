"""Compatibility import target for the split health route tests.

The AWF targeted validation command still addresses the historical route-level
path. Default discovery ignores this shim through ``tests.conftest`` so it
collects the canonical split tests only once, while direct file targeting still
collects this compatibility import.
"""

from tests.unit.api.test_health_parts.test_health_part_001 import *  # noqa: F403
