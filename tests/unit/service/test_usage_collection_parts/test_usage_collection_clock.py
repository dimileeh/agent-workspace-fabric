"""Clock construction coverage for the ccusage collector."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.service.usage_collection import CcusageCollector, _RealClock


@pytest.mark.unit
async def test_real_clock_defaults(tmp_path: Path) -> None:
    """The collector uses a real clock unless tests provide a fake one."""
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path)
    assert isinstance(collector._clock, _RealClock)
    assert isinstance(collector._clock.now(), datetime)
    await collector._clock.sleep(0)
