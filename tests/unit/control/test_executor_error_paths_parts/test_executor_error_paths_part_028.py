"""Executor PR-monitor handoff compose runtime usability coverage (split from part 018)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.executor import monitor_handoff as monitor_handoff_module
from awf.runtime.inspection import RuntimeService


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stack_state", "expected"),
    [
        ("running", True),
        ("stopped", False),
        ("unavailable", False),
        ("unknown", False),
    ],
)
async def test_compose_runtime_usable_after_restart_failure_requires_running_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stack_state: str,
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state=stack_state,
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                ),
                RuntimeService(
                    name="postgres",
                    container_id="postgres-id",
                    image="postgres",
                    state="running",
                ),
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
        )
        is expected
    )


@pytest.mark.unit
async def test_compose_runtime_usable_after_restart_failure_rejects_inspection_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        raise RuntimeError("docker inspect unavailable")

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_x",
        compose_file,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "compose_text",
    [
        None,
        "services: [\n",
    ],
)
async def test_compose_runtime_usable_after_restart_failure_allows_unreadable_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compose_text: str | None,
) -> None:
    compose_file = tmp_path / "compose.yml"
    if compose_text is not None:
        compose_file.write_text(compose_text, encoding="utf-8")

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                )
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_x",
        compose_file,
    )


@pytest.mark.unit
async def test_compose_runtime_usable_after_restart_failure_rejects_partial_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                )
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert not await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
        "awf_x",
        compose_file,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ignored_services", "expected"),
    [(frozenset(), False), (frozenset({"agent"}), True)],
)
async def test_compose_runtime_usable_after_restart_failure_skips_ignored_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ignored_services: frozenset[str],
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="exited",
                ),
                RuntimeService(
                    name="postgres",
                    container_id="postgres-id",
                    image="postgres",
                    state="running",
                ),
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
            ignored_services=ignored_services,
        )
        is expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("service", "active_profiles", "expected"),
    [
        (None, set(), True),
        ({}, set(), True),
        ({"profiles": []}, set(), True),
        ({"profiles": "debug"}, set(), True),
        ({"profiles": [""]}, set(), True),
        ({"profiles": ["debug"]}, set(), False),
        ({"profiles": ["debug"]}, {"debug"}, True),
        ({"profiles": ["debug"]}, {"*"}, True),
    ],
)
def test_compose_service_enabled_for_active_profiles(
    service: object,
    active_profiles: set[str],
    expected: bool,
) -> None:
    assert (
        monitor_handoff_module._compose_service_enabled_for_active_profiles(
            service,
            active_profiles=active_profiles,
        )
        is expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("active_profiles", "expected"),
    [
        (None, True),
        ("metrics", True),
        ("debug", False),
        ("*", False),
    ],
)
async def test_compose_runtime_usable_after_restart_failure_honors_active_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_profiles: str | None,
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  agent:
    image: awf-agent
  debug:
    image: awf-debug
    profiles:
      - debug
""",
        encoding="utf-8",
    )
    if active_profiles is None:
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    else:
        monkeypatch.setenv("COMPOSE_PROFILES", active_profiles)

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                )
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
        )
        is expected
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("oneshot_status", "expected"),
    [
        ("Exited (0) 2 minutes ago", True),
        ("Exited (1) 2 minutes ago", False),
    ],
)
async def test_compose_runtime_usable_after_restart_failure_handles_completed_oneshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    oneshot_status: str,
    expected: bool,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  migrate:
    image: awf-migrate
  agent:
    image: awf-agent
    depends_on:
      migrate:
        condition: service_completed_successfully
  postgres:
    image: postgres
""",
        encoding="utf-8",
    )

    async def _inspect(_compose_project: str) -> monitor_handoff_module.RuntimeSnapshot:
        return monitor_handoff_module.RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="migrate",
                    container_id="migrate-id",
                    image="awf-migrate",
                    state="exited",
                    status=oneshot_status,
                ),
                RuntimeService(
                    name="agent",
                    container_id="agent-id",
                    image="awf-agent",
                    state="running",
                ),
                RuntimeService(
                    name="postgres",
                    container_id="postgres-id",
                    image="postgres",
                    state="running",
                ),
            ],
        )

    monkeypatch.setattr(monitor_handoff_module, "_inspect_compose_runtime", _inspect)

    assert (
        await monitor_handoff_module._compose_runtime_usable_after_restart_failure(
            "awf_x",
            compose_file,
        )
        is expected
    )
