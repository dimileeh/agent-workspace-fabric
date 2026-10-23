"""Companions whose env the resume refresh rewrote are recreated before the agent (#936).

The resume brings dependencies up with ``--no-recreate``, so a compose.yml
environment rewrite never reaches an existing companion container unless the
resume force-recreates that companion (``--no-deps``). It does so first, before
anything is waited on, and recreates the agent last.
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
from awf.node.compose_manager import ComposeOperationError
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
async def test_restart_force_recreates_rewritten_companions_before_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inspections: list[int] = []

    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        # The recreated companions are waited on, never started: ``worker`` exits again.
        inspections.append(len(compose.calls))
        backend_health = "starting" if len(inspections) == 1 else "healthy"
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="backend",
                    container_id="b",
                    image=None,
                    state="running",
                    health=backend_health,
                ),
                RuntimeService(name="worker", container_id="w", image=None, state="exited"),
                RuntimeService(
                    name="postgres", container_id="p", image=None, state="running", health="healthy"
                ),
                RuntimeService(name="agent", container_id="a", image=None, state="running"),
            ],
        )

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", _inspect)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", _no_sleep)
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

    assert inspections == [1, 1]
    assert [(call.get("services"), call.get("force_recreate")) for call in compose.calls] == [
        (("backend", "worker"), True),
        (("backend", "postgres", "worker"), None),
        (("agent",), True),
    ]
    assert compose.calls[0]["no_deps"] is True
    assert compose.calls[0]["wait"] is False
    assert compose.calls[2]["no_deps"] is True


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
    assert [call["services"] for call in recreate] == [("backend",), ("agent",)]
    assert [call.get("services") for call in compose.calls if call.get("no_recreate")] == [
        ("backend", "worker")
    ]


@pytest.mark.unit
def test_companions_with_a_set_env_secret_are_reported_even_when_compose_is_unchanged() -> None:
    specs = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "api",
                    "repo_url": "git@github.com:x/api.git",
                    "environment_secrets": {
                        "API_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "API_TOKEN_SOURCE",
                            "required": True,
                        }
                    },
                },
                *(
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
                ),
                {"name": "plain", "repo_url": "git@github.com:x/plain.git"},
            ]
        }
    )

    # A rotated value keeps the same ``${SOURCE}`` placeholder, so set sources count.
    assert monitor_handoff._companions_with_set_env_secrets_for_resume(
        companion_specs=specs,
        environ={"API_TOKEN_SOURCE": "rotated", "OPTIONAL_TOKEN_SOURCE": "rotated"},
    ) == ("api", "backend", "worker")
    assert monitor_handoff._companions_with_set_env_secrets_for_resume(
        companion_specs=specs, environ={"API_TOKEN_SOURCE": "rotated"}
    ) == ("api",)


@pytest.mark.unit
async def test_resume_recreates_declared_companions_with_set_env_secrets(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))

    def _nothing_rewritten(**_kwargs: Any) -> tuple[str, ...]:
        return ()

    def _secret_companions(**_kwargs: Any) -> tuple[str, ...]:
        # ``ghost`` has no compose service, so it is never handed to compose.
        return ("ghost", "worker")

    monkeypatch.setattr(
        monitor_handoff, "_refresh_optional_companion_env_secrets_for_resume", _nothing_rewritten
    )
    monkeypatch.setattr(
        monitor_handoff, "_companions_with_set_env_secrets_for_resume", _secret_companions
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
    assert [call["services"] for call in recreate] == [("worker",), ("agent",)]
    assert [call.get("services") for call in compose.calls if call.get("no_recreate")] == [
        ("backend", "worker")
    ]


@pytest.mark.unit
async def test_resume_recreates_companions_with_a_removed_optional_source_when_rewrite_fails(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")
    ws_id = await _seed_monitoring_pr(
        factory,
        compose_file_path=str(compose_file),
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
            ]
        },
    )

    def _write_fails(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    monkeypatch.setattr(monitor_handoff._companion_env, "_atomic_write_text", _write_fails)
    compose = _RecordingCompose()
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
    )

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    # The unpersisted removal leaves ``${OPTIONAL_TOKEN_SOURCE:-}`` in compose.yml,
    # so only a recreate drops the revoked value from the running ``backend``.
    assert "${OPTIONAL_TOKEN_SOURCE:-}" in compose_file.read_text(encoding="utf-8")
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("backend",), ("agent",)]


@pytest.mark.unit
async def test_resume_leaves_companions_alone_when_an_unset_optional_source_is_already_omitted(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        _COMPOSE.replace(
            '    environment:\n      OPTIONAL_TOKEN: "${OPTIONAL_TOKEN_SOURCE:-}"\n', ""
        ),
        encoding="utf-8",
    )
    ws_id = await _seed_monitoring_pr(
        factory,
        compose_file_path=str(compose_file),
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
            ]
        },
    )
    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    compose = _RecordingCompose()
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
    )

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    # An unused optional secret is the steady state: nothing in compose.yml to clear.
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("agent",)]


@pytest.mark.unit
def test_companions_still_carrying_an_unset_optional_target_are_reported(
    tmp_path: Path,
) -> None:
    def _stale(compose_file: Path, environ: dict[str, str]) -> tuple[str, ...]:
        return monitor_handoff._companions_with_stale_optional_env_secret_targets_for_resume(
            compose_file=compose_file,
            companion_specs=_specs(),
            environ=environ,
        )

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")
    # ``backend`` still carries ``${OPTIONAL_TOKEN_SOURCE:-}``; ``worker`` never had it.
    assert _stale(compose_file, {}) == ("backend",)
    assert _stale(compose_file, {"OPTIONAL_TOKEN_SOURCE": "x"}) == ()
    assert _stale(tmp_path / "missing.yml", {}) == ()
    unparsable = tmp_path / "unparsable.yml"
    unparsable.write_text("services: [\n", encoding="utf-8")
    assert _stale(unparsable, {}) == ()


class _CompanionRecreateFailsOnce(_RecordingCompose):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def ensure_project_up(self, **kwargs: Any) -> None:
        await super().ensure_project_up(**kwargs)
        companion = kwargs.get("services") != ("agent",)
        if kwargs.get("force_recreate") and companion and not self.failed:
            self.failed = True
            raise ComposeOperationError(
                operation="up",
                returncode=1,
                stdout="",
                stderr="recreate failed",
                reason_code="COMPOSE_UP_FAILED",
            )


@pytest.mark.unit
async def test_resume_retries_a_companion_recreate_that_failed_after_its_env_was_rewritten(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")
    ws_id = await _seed_monitoring_pr(
        factory,
        compose_file_path=str(compose_file),
        task_policy={
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@github.com:x/backend.git",
                    "environment_secrets": {
                        "OPTIONAL_TOKEN": {
                            "provider": "env",
                            "kind": "env",
                            "value_from": "OPTIONAL_TOKEN_SOURCE",
                            "required": False,
                        }
                    },
                }
            ]
        },
    )
    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    compose = _CompanionRecreateFailsOnce()

    def _executor() -> Any:
        return _make_executor(
            fake,
            factory,
            tmp_path,
            compose=compose,
            pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
        )

    # The refresh persists the omission, then the companion recreate fails.
    assert await _executor().resume_pr_monitor_handoff(ws_id) is None
    assert "OPTIONAL_TOKEN" not in compose_file.read_text(encoding="utf-8")

    # A replacement executor still recreates ``backend``: compose.yml no longer
    # shows the omission, but the running container keeps the revoked value.
    compose.calls.clear()
    assert await _executor().resume_pr_monitor_handoff(ws_id) is not None
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("backend",), ("agent",)]

    # Once recreated, the omitted target is the steady state again.
    compose.calls.clear()
    assert await _executor().resume_pr_monitor_handoff(ws_id) is not None
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("agent",)]


@pytest.mark.unit
def test_pending_companion_recreates_tolerate_an_unreadable_or_stuck_marker(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    marker = monitor_handoff._companion_env._pending_companion_recreates_path(compose_file)
    assert (
        monitor_handoff._pending_companion_recreates_for_resume(
            workspace_id="ws", compose_file=compose_file
        )
        == ()
    )

    # An unreadable marker cannot name the services, so it reports none.
    marker.mkdir()
    assert (
        monitor_handoff._pending_companion_recreates_for_resume(
            workspace_id="ws", compose_file=compose_file
        )
        == ()
    )
    # A marker that cannot be removed is kept, so the next resume recreates again.
    monitor_handoff._clear_pending_companion_recreates_for_resume(
        workspace_id="ws", compose_file=compose_file
    )
    assert marker.is_dir()


class _AgentRecreateFailsOnce(_RecordingCompose):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def ensure_project_up(self, **kwargs: Any) -> None:
        await super().ensure_project_up(**kwargs)
        if kwargs.get("services") == ("agent",) and not self.failed:
            self.failed = True
            raise ComposeOperationError(
                operation="up",
                returncode=1,
                stdout="",
                stderr="agent recreate failed",
                reason_code="COMPOSE_UP_FAILED",
            )


@pytest.mark.unit
async def test_resume_clears_the_pending_recreate_once_companions_were_recreated(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(_COMPOSE, encoding="utf-8")
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))
    marker = monitor_handoff._companion_env._pending_companion_recreates_path(compose_file)
    marker.write_text("backend\n", encoding="utf-8")
    compose = _AgentRecreateFailsOnce()

    def _executor() -> Any:
        return _make_executor(
            fake,
            factory,
            tmp_path,
            compose=compose,
            pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
        )

    # ``backend`` is recreated, then the agent recreate fails the restart.
    assert await _executor().resume_pr_monitor_handoff(ws_id) is None
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("backend",), ("agent",)]
    assert not marker.exists()

    # The retry recreates only the agent: the recovered ``backend`` is left alone (#936).
    compose.calls.clear()
    assert await _executor().resume_pr_monitor_handoff(ws_id) is not None
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("agent",)]


@pytest.mark.unit
async def test_resume_keeps_pending_recreates_for_companions_it_could_not_recreate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        _COMPOSE + "  extra:\n    image: extra\n    profiles: [extra]\n", encoding="utf-8"
    )
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))
    marker = monitor_handoff._companion_env._pending_companion_recreates_path(compose_file)
    marker.write_text("backend\nextra\n", encoding="utf-8")
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    compose = _RecordingCompose()

    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
    )

    # ``extra`` is outside the active profiles, so only ``backend`` is recreated
    # and ``extra`` stays pending until a resume can recreate it.
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("backend",), ("agent",)]
    assert marker.read_text(encoding="utf-8") == "extra\n"


@pytest.mark.unit
async def test_resume_leaves_the_pending_recreate_alone_when_nothing_was_recreated(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        _COMPOSE + "  extra:\n    image: extra\n    profiles: [extra]\n", encoding="utf-8"
    )
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))
    marker = monitor_handoff._companion_env._pending_companion_recreates_path(compose_file)
    marker.write_text("extra\n", encoding="utf-8")
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    compose = _RecordingCompose()

    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_a, **_k: _IdleMonitor(),
    )

    # The only pending companion is outside the active profiles: no dependency is
    # force-recreated, so the marker must not be cleared or rewritten.
    clears: list[dict[str, object]] = []
    monkeypatch.setattr(
        monitor_handoff,
        "_clear_pending_companion_recreates_for_resume",
        lambda **kwargs: clears.append(kwargs),
    )
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    recreate = [call for call in compose.calls if call.get("force_recreate")]
    assert [call["services"] for call in recreate] == [("agent",)]
    assert clears == []
    assert marker.read_text(encoding="utf-8") == "extra\n"
