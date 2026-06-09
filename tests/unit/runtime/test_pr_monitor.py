"""Compatibility fixtures for PR monitor unit tests.

The original oversized test module was split into focused parts. A few sibling
tests still share the small status/thread factories, so keep those helpers here
instead of rebuilding another large test module.
"""

from __future__ import annotations

from tests.unit.runtime.test_pr_monitor_parts.test_pr_monitor_part_001 import (
    _status,
    _thread,
)

__all__ = ["_status", "_thread"]
