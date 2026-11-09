"""``ValidationRunner`` holds the caller-injected work slot (slot-release).

``run_profile_phases`` and ``run_profile_coverage`` hold the slot for the whole
validation run; every phase command and the coverage command run as
``awf-exec`` tagged with ``AWF_WORK_SLOT_INVOCATION_ID``. Short-lived healthcheck
probes stay unmarked, like toolchain probes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.work_slot import WORK_SLOT_INVOCATION_ENV
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationRunner

pytestmark = pytest.mark.unit

_COMPOSE_PROJECT = "awf_ws_val_slot"
_COMPOSE_FILE = Path("/fake/compose.yml")
_MARKER = f"{WORK_SLOT_INVOCATION_ENV}=wsl_validation"


@dataclass
class _Handle:
    invocation_id: str


@dataclass
class _RecordingWorkSlot:
    events: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def hold(self, phase: str) -> AsyncIterator[_Handle]:
        self.events.append(f"enter:{phase}")
        try:
            yield _Handle("wsl_validation")
        finally:
            self.events.append(f"exit:{phase}")

    async def release_idle(self) -> None:  # pragma: no cover - not used here
        self.events.append("release_idle")


def _profile() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "slot-profile",
            "phases": {"setup": ["make deps"], "validate": ["pytest -q", "ruff check ."]},
            "validation": {
                "coverage": {
                    "minimum_percent": 10.0,
                    "command": "coverage run -m pytest && coverage report",
                },
            },
        }
    )


async def test_profile_phases_hold_the_slot_and_tag_every_phase_exec(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    slot = _RecordingWorkSlot()
    runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    await runner.run_profile_phases(
        workspace_id="ws_slot",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_profile(),
        phase_names=("setup", "validate"),
        work_slot=slot,
    )

    assert slot.events == ["enter:validation", "exit:validation"]
    # setup + 2 validate commands + the coverage command.
    assert len(fake.calls) == 4
    assert all(_MARKER in call.args for call in fake.calls)


async def test_profile_coverage_holds_the_slot_and_tags_the_coverage_exec(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    slot = _RecordingWorkSlot()
    runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    await runner.run_profile_coverage(
        workspace_id="ws_slot",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_profile(),
        work_slot=slot,
    )

    assert slot.events == ["enter:validation", "exit:validation"]
    assert len(fake.calls) == 1
    assert _MARKER in fake.calls[0].args


async def test_coverage_not_requested_holds_no_slot(tmp_path: Path) -> None:
    slot = _RecordingWorkSlot()
    runner = ValidationRunner(runner=FakeCommandRunner(), artifacts_dir=tmp_path / "artifacts")

    result = await runner.run_profile_coverage(
        workspace_id="ws_slot",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=WorkspaceProfile.model_validate({"name": "no-coverage"}),
        work_slot=slot,
    )

    assert result is None
    assert slot.events == []


async def test_without_a_work_slot_validation_execs_are_unmarked(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    await runner.run_profile_phases(
        workspace_id="ws_plain",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_profile(),
        phase_names=("validate",),
    )

    assert fake.calls
    assert not any(WORK_SLOT_INVOCATION_ENV in arg for call in fake.calls for arg in call.args)
