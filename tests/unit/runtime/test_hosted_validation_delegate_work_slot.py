"""``HostedValidationDelegate`` holds the caller-injected work slot (slot-release).

Hosted validation runs remotely, so there is no ``awf-exec`` to tag: the hold
gates the slot for the duration of the delegated operation only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedValidationDelegate
from tests.unit.runtime.test_hosted_validation_delegate import _config

pytestmark = pytest.mark.unit


@dataclass
class _Handle:
    invocation_id: str


@dataclass
class _RecordingWorkSlot:
    events: list[str] = field(default_factory=list)
    active: bool = False

    @asynccontextmanager
    async def hold(self, phase: str) -> AsyncIterator[_Handle]:
        self.events.append(f"enter:{phase}")
        self.active = True
        try:
            yield _Handle("wsl_hosted")
        finally:
            self.active = False
            self.events.append(f"exit:{phase}")

    async def release_idle(self) -> None:  # pragma: no cover - not used here
        self.events.append("release_idle")


def _handler(slot: _RecordingWorkSlot, seen: list[bool]) -> Any:
    async def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(slot.active)
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                    "coverage": {
                        "provider": "python",
                        "percent": 99.0,
                        "minimum_percent": 99.0,
                        "enforce": True,
                        "status": "passed",
                        "reason_code": "COVERAGE_OK",
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return _handle


async def test_hosted_phases_and_coverage_run_inside_the_work_slot(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    slot = _RecordingWorkSlot()
    seen: list[bool] = []
    profile = WorkspaceProfile.model_validate({"name": "hosted-slot"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler(slot, seen))) as client:
        delegate = HostedValidationDelegate(
            _config(), artifacts_dir=tmp_path / "artifacts", client=client
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=profile,
            phase_names=("validate",),
            include_coverage=False,
            work_slot=slot,
        )
        await delegate.run_profile_coverage(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=profile,
            work_slot=slot,
        )

    assert seen and all(seen)
    assert slot.events == [
        "enter:validation",
        "exit:validation",
        "enter:validation",
        "exit:validation",
    ]
