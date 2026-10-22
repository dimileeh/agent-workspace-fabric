"""Companions whose env the resume refresh rewrote are recreated with the agent (#936).

The resume brings dependencies up with ``--no-recreate``, so a compose.yml
environment rewrite never reaches an existing companion container unless the
resume force-recreates that companion (``--no-deps``) together with the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff, monitor_resume_runtime
from awf.node.companion_services import WorkspaceCompanionSpec, companion_specs_from_task_policy
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)

_IMPORTED_FIXTURES = (factory, fake)

_COMPOSE = """
services:
  agent:
    image: agent
  backend:
    image: backend
    environment:
      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"
  worker:
    image: worker
    environment:
      OTHER: "1"
""".lstrip()


def _specs() -> tuple[WorkspaceCompanionSpec, ...]:
    return companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": name,
                    "repo_url": f"git@github.com:x/{name}.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
                for name in ("backend", "worker")
            ]
        }
    )


def _refresh(compose_file: Path, environ: dict[str, str]) -> tuple[str, ...]:
    return monitor_handoff._refresh_optional_companion_env_secrets_for_resume(
        workspace_id="ws",
        compose_file=compose_file,
        companion_specs=_specs(),
        environ=environ,
    )


@pytest.mark.unit
def test_refresh_returns_only_the_services_it_rewrote(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")

    # ``backend`` loses its now-unset optional secret; ``worker`` gains the set one.
    assert _refresh(compose_file, {}) == ("backend",)
    assert _refresh(compose_file, {"OPTIONAL_TOKEN_SOURCE": "x"}) == ("backend", "worker")
    # Nothing left to rewrite.
    assert _refresh(compose_file, {"OPTIONAL_TOKEN_SOURCE": "x"}) == ()


@pytest.mark.unit
def test_refresh_reports_nothing_when_compose_file_cannot_be_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.yml"
    assert _refresh(missing, {}) == ()
    no_services = tmp_path / "no-services.yml"
    no_services.write_text("services: []\n", encoding="utf-8")
    assert _refresh(no_services, {}) == ()

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")

    def _write_fails(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(monitor_handoff._companion_env, "_atomic_write_text", _write_fails)
    assert _refresh(compose_file, {}) == ()


class _RecordingCompose:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.unit
async def test_restart_force_recreates_rewritten_companions_with_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        # A recreated companion is neither waited on nor started beforehand.
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="backend", container_id="b", image=None, state="running", health="starting"
                ),
                RuntimeService(name="worker", container_id="w", image=None, state="exited"),
                RuntimeService(name="agent", container_id="a", image=None, state="running"),
            ],
        )

    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", _inspect)
    compose = _RecordingCompose()

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=300,
        dependency_services=("agent", "backend", "postgres", "worker"),
        recreate_services=("worker", "backend"),
    )

    assert [(call.get("services"), call.get("force_recreate")) for call in compose.calls] == [
        (("postgres",), None),
        (("agent", "backend", "worker"), True),
    ]
    assert compose.calls[1]["no_deps"] is True


class _IdleMonitor:
    async def run(self, **_kwargs: Any) -> None:
        return None


@pytest.mark.unit
async def test_resume_recreates_the_companions_the_refresh_rewrote(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))

    def _rewrote(**_kwargs: Any) -> tuple[str, ...]:
        return ("backend",)

    monkeypatch.setattr(
        monitor_handoff, "_refresh_optional_companion_env_secrets_for_resume", _rewrote
    )
    compose = _RecordingCompose()
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
    )

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("agent", "backend")]
    assert [call.get("services") for call in compose.calls if call.get("no_recreate")] == [
        ("worker",)
    ]
