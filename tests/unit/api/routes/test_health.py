"""Compatibility target for the split health route tests.

The AWF targeted validation command still addresses the historical route-level
path. Keep this file as a collection shim for the changed canonical part.
"""

from tests.unit.api.test_health_parts.test_health_part_001 import *  # noqa: F403
