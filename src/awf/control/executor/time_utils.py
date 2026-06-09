"""Executor clock helpers."""

from __future__ import annotations

import time


def _monotonic() -> float:
    return time.monotonic()
