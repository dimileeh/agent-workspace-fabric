"""Compose flags and healthcheck budget behind monitor resume dependency recovery (#936).

A monitor resume must recreate only the agent and leave running or recovering
dependencies alone (``--no-recreate`` for the project, ``--no-deps`` for the agent),
and the rendered dependency healthcheck budget follows the stack's startup budget
instead of a fixed 60 retries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node import compose_manager as compose_module
from awf.node.compose_manager import ComposeManager, ComposeService, WorkspaceComposeSpec

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


class _FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
        calls.append(args)
        return _FakeProcess()

    monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)
    return calls


def _postgres_spec(tmp_path: Path, **overrides: object) -> WorkspaceComposeSpec:
    return WorkspaceComposeSpec(
        workspace_id="ws_budget",
        worktree_host_path=tmp_path / "wt",
        services=(
            ComposeService(
                name="postgres",
                image="postgres:16-alpine",
                healthcheck_cmd="pg_isready -U awf",
            ),
        ),
        **overrides,  # type: ignore[arg-type]
    )


def _postgres_healthcheck(manager: ComposeManager, spec: WorkspaceComposeSpec) -> dict[str, object]:
    paths = manager.render(spec)
    payload = yaml.safe_load(paths.compose_file.read_text(encoding="utf-8"))
    healthcheck = payload["services"]["postgres"]["healthcheck"]
    assert isinstance(healthcheck, dict)
    return healthcheck


@pytest.mark.unit
async def test_no_recreate_adds_flag_without_force_recreate(
    manager: ComposeManager, tmp_path: Path, spawned: list[tuple[object, ...]]
) -> None:
    await manager.ensure_project_up(
        project_name="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        compose_up_timeout_seconds=900,
        no_recreate=True,
    )

    assert spawned == [
        (
            "docker",
            "compose",
            "up",
            "-d",
            "--remove-orphans",
            "--no-recreate",
            "--wait",
            "--wait-timeout",
            "900",
        )
    ]


@pytest.mark.unit
async def test_agent_only_recreate_adds_no_deps_before_service(
    manager: ComposeManager, tmp_path: Path, spawned: list[tuple[object, ...]]
) -> None:
    await manager.ensure_project_up(
        project_name="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        force_recreate=True,
        no_deps=True,
        services=("agent",),
    )

    assert spawned == [
        (
            "docker",
            "compose",
            "up",
            "-d",
            "--remove-orphans",
            "--force-recreate",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "300",
            "agent",
        )
    ]


@pytest.mark.unit
async def test_force_recreate_and_no_recreate_are_mutually_exclusive(
    manager: ComposeManager, tmp_path: Path, spawned: list[tuple[object, ...]]
) -> None:
    with pytest.raises(ValueError, match="force_recreate"):
        await manager.ensure_project_up(
            project_name="awf_ws",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws",
            force_recreate=True,
            no_recreate=True,
        )

    assert spawned == []


@pytest.mark.unit
async def test_default_ensure_project_up_argv_is_unchanged(
    manager: ComposeManager, tmp_path: Path, spawned: list[tuple[object, ...]]
) -> None:
    await manager.ensure_project_up(
        project_name="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
    )

    assert spawned == [
        ("docker", "compose", "up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "300")
    ]


@pytest.mark.unit
def test_default_budget_renders_todays_healthcheck(manager: ComposeManager, tmp_path: Path) -> None:
    healthcheck = _postgres_healthcheck(manager, _postgres_spec(tmp_path))

    assert healthcheck["interval"] == "5s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["retries"] == 60
    assert healthcheck["start_period"] == "5s"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("budget_seconds", "retries"),
    [(900, 180), (301, 61), (1, 1), (7200, 1440)],
)
def test_healthcheck_retries_follow_the_startup_budget(
    manager: ComposeManager, tmp_path: Path, budget_seconds: int, retries: int
) -> None:
    healthcheck = _postgres_healthcheck(
        manager, _postgres_spec(tmp_path, compose_up_timeout_seconds=budget_seconds)
    )

    assert healthcheck["retries"] == retries
    assert healthcheck["interval"] == "5s"
    assert healthcheck["start_period"] == "5s"
