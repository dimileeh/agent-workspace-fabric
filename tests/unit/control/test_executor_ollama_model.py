"""Executor pre-agent Ollama discovery + auto-pull step (issue #552).

Covers ``_ensure_ollama_model_or_mark_failed``: an OpenCode workspace whose model
is auto-pulled proceeds, a pull failure marks the workspace failed with the clear
``OLLAMA_MODEL_PULL_FAILED`` reason code (never ``AGENT_CLI_FAILED``), and
non-OpenCode runtimes are a no-op.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.adapters.base import AgentDefaults
from awf.adapters.opencode import OPENCODE_OLLAMA_CLOUD_MODELS
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    ollama_model,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _make_executor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    default_models: dict[Any, str] | None = None,
    agent_defaults: dict[AgentRuntime, AgentDefaults] | None = None,
) -> WorkspaceExecutor:
    fake = FakeCommandRunner()
    config_kwargs: dict[str, Any] = {
        "worktrees_root": tmp_path / "work" / "worktrees",
        "compose_projects_root": tmp_path / "work" / "compose",
        "default_models": default_models,
    }
    if agent_defaults is not None:
        config_kwargs["agent_defaults"] = agent_defaults
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=SimpleNamespace(),
        validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
        pr_creator=PullRequestCreator(fake),
        config=ExecutorConfig(**config_kwargs),
    )


async def _seed_running(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "opencode",
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="ollama",
            task_prompt="p",
            agent=agent,
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await s.commit()
        return ws.id


async def _get_status(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> Any:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        return SimpleNamespace(
            status=ws.status,
            failure_reason=ws.failure_reason,
            failure_message=ws.failure_message,
            events=[(e.event_type, e.reason_code) for e in ws.events],
        )


@pytest.mark.unit
async def test_opencode_pull_success_proceeds(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    def _stub(*, on_progress: Any = None, **_kwargs: Any) -> dict[str, Any]:
        if on_progress is not None:
            on_progress("pulling manifest")
        return {
            "status": "ok",
            "reason_code": "OLLAMA_MODEL_PULLED",
            "message": "pulled",
        }

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={"agent_model": "ollama/llama4:70b"}),
    )

    assert proceed is True
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.running.value
    assert ("workspace.ollama_model_pulled", "OLLAMA_MODEL_PULLED") in snap.events


@pytest.mark.unit
async def test_opencode_already_available_proceeds_without_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setattr(
        ollama_model,
        "ensure_ollama_model_available",
        lambda **_kwargs: {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"},
    )

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={}),
    )

    assert proceed is True
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.running.value
    assert not any(evt == "workspace.ollama_model_pulled" for evt, _ in snap.events)


@pytest.mark.unit
async def test_opencode_pull_failure_marks_failed_not_agent_cli_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setattr(
        ollama_model,
        "ensure_ollama_model_available",
        lambda **_kwargs: {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_PULL_FAILED",
            "message": "Ollama pull of 'llama4:70b' did not complete successfully.",
            "detail": "pull model manifest: file does not exist",
        },
    )

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={"agent_model": "ollama/llama4:70b"}),
    )

    assert proceed is False
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.failed.value
    assert snap.failure_reason == "infrastructure_failure"
    reason_codes = [reason for _, reason in snap.events]
    assert "OLLAMA_MODEL_PULL_FAILED" in reason_codes
    assert "AGENT_CLI_FAILED" not in reason_codes


@pytest.mark.unit
async def test_opencode_probe_failure_without_detail_marks_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setattr(
        ollama_model,
        "ensure_ollama_model_available",
        lambda **_kwargs: {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_PROBE_FAILED",
            "message": "Ollama model availability probe did not complete successfully.",
        },
    )

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={}),
    )

    assert proceed is False
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.failed.value
    assert "OLLAMA_MODEL_PROBE_FAILED" in [reason for _, reason in snap.events]


@pytest.mark.unit
async def test_ensure_resolves_executor_config_model_override(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit ``agent_model``, ensure validates the ExecutorConfig
    default-model override the adapter will actually run, not the static default."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(
        factory,
        tmp_path,
        default_models={AgentRuntime.opencode: "ollama/custom-glm:32b"},
    )

    seen: dict[str, Any] = {}

    def _stub(*, model: Any = None, **_kwargs: Any) -> dict[str, Any]:
        seen["model"] = model
        return {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={}),
    )

    assert proceed is True
    assert seen["model"] == "ollama/custom-glm:32b"


@pytest.mark.unit
async def test_ensure_falls_back_to_adapter_cloud_default_when_no_model_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither an explicit ``agent_model`` nor an adapter default resolves a
    model, the preflight must mirror the OpenCode adapter's final fallback to
    ``OPENCODE_OLLAMA_CLOUD_MODELS[0]`` — probing/pulling the model the agent will
    actually launch — instead of failing the workspace with ``MODEL_NOT_SELECTED``."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path, agent_defaults={})

    seen: dict[str, Any] = {}

    def _stub(*, model: Any = None, **_kwargs: Any) -> dict[str, Any]:
        seen["model"] = model
        return {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={}),
    )

    assert proceed is True
    assert seen["model"] == OPENCODE_OLLAMA_CLOUD_MODELS[0]
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.running.value


@pytest.mark.unit
@pytest.mark.parametrize(
    "model",
    ["openai/gpt-oss", "anthropic/claude-sonnet"],
)
async def test_opencode_non_ollama_provider_skips_preflight(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    """A provider-qualified non-Ollama model must skip the Ollama preflight so
    OpenCode can use the selected provider instead of failing with an Ollama
    reason after a bogus pull from the local daemon."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    def _must_not_run(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("ensure_ollama_model_available must not run for non-Ollama provider")

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _must_not_run)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={"agent_model": model}),
    )

    assert proceed is True
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.running.value


@pytest.mark.unit
async def test_ensure_derives_urls_from_profile_ollama_base_url(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe/pull URLs must come from the agent/profile Ollama base URL, not
    the worker process env: a profile that sets ``AWF_OPENCODE_OLLAMA_BASE_URL``
    for the agent points at a different daemon than the worker, and AWF must
    probe/pull the daemon the agent will actually reach."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setenv("AWF_OPENCODE_OLLAMA_BASE_URL", "http://worker-daemon:11434")

    seen: dict[str, Any] = {}

    def _stub(*, tags_urls: Any = None, pull_urls: Any = None, **_kwargs: Any) -> dict[str, Any]:
        seen["tags_urls"] = tags_urls
        seen["pull_urls"] = pull_urls
        return {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(
            agent="opencode",
            task_policy={"agent_model": "ollama/llama4:70b"},
            resolved_profile={
                "name": "ollama-sidecar",
                "runtime": {
                    "environment": {
                        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434",
                    }
                },
            },
        ),
    )

    assert proceed is True
    assert any("ollama-sidecar:11434" in url for url in seen["tags_urls"])
    assert any("ollama-sidecar:11434" in url for url in seen["pull_urls"])
    assert all("worker-daemon" not in url for url in seen["tags_urls"])


@pytest.mark.unit
async def test_ensure_without_resolved_profile_uses_worker_env(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a resolved profile the preflight falls back to the worker env so
    the existing single-daemon behavior is preserved."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setenv("AWF_OPENCODE_OLLAMA_BASE_URL", "http://worker-daemon:11434")

    seen: dict[str, Any] = {}

    def _stub(*, tags_urls: Any = None, **_kwargs: Any) -> dict[str, Any]:
        seen["tags_urls"] = tags_urls
        return {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="opencode", task_policy={"agent_model": "ollama/llama4:70b"}),
    )

    assert proceed is True
    assert any("worker-daemon:11434" in url for url in seen["tags_urls"])


@pytest.mark.unit
async def test_ensure_profile_ollama_host_overrides_worker_base_url(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile that declares only ``OLLAMA_HOST`` must win over a stale worker
    ``AWF_OPENCODE_OLLAMA_BASE_URL``: ``_ollama_api_urls`` prefers the base URL,
    so the worker value would otherwise shadow the profile-selected daemon and
    AWF would probe/pull the wrong host."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setenv("AWF_OPENCODE_OLLAMA_BASE_URL", "http://worker-daemon:11434")

    seen: dict[str, Any] = {}

    def _stub(*, tags_urls: Any = None, pull_urls: Any = None, **_kwargs: Any) -> dict[str, Any]:
        seen["tags_urls"] = tags_urls
        seen["pull_urls"] = pull_urls
        return {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(
            agent="opencode",
            task_policy={"agent_model": "ollama/llama4:70b"},
            resolved_profile={
                "name": "ollama-sidecar",
                "runtime": {
                    "environment": {
                        "OLLAMA_HOST": "http://ollama-sidecar:11434",
                    }
                },
            },
        ),
    )

    assert proceed is True
    assert any("ollama-sidecar:11434" in url for url in seen["tags_urls"])
    assert any("ollama-sidecar:11434" in url for url in seen["pull_urls"])
    assert all("worker-daemon" not in url for url in seen["tags_urls"])
    assert all("worker-daemon" not in url for url in seen["pull_urls"])


@pytest.mark.unit
async def test_ensure_expands_profile_ollama_placeholder_against_environ(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile that declares the Ollama endpoint through a Compose-style
    ``${NAME}`` placeholder must be resolved against the worker environ before the
    probe/pull URLs are built. The agent container expands such placeholders via
    Docker Compose host-env substitution, but the worker-side probe does not pass
    through Compose — so a literal ``${OLLAMA_URL}`` would otherwise be probed as
    ``http://${OLLAMA_URL}/api/tags`` and block/fail the workspace before launch."""
    workspace_id = await _seed_running(factory)
    executor = _make_executor(factory, tmp_path)

    monkeypatch.setenv("OLLAMA_URL", "http://ollama-sidecar:11434")

    seen: dict[str, Any] = {}

    def _stub(*, tags_urls: Any = None, pull_urls: Any = None, **_kwargs: Any) -> dict[str, Any]:
        seen["tags_urls"] = tags_urls
        seen["pull_urls"] = pull_urls
        return {"status": "ok", "reason_code": "OLLAMA_MODEL_AVAILABLE"}

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _stub)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(
            agent="opencode",
            task_policy={"agent_model": "ollama/llama4:70b"},
            resolved_profile={
                "name": "ollama-sidecar",
                "runtime": {
                    "environment": {
                        "AWF_OPENCODE_OLLAMA_BASE_URL": "${OLLAMA_URL}",
                    }
                },
            },
        ),
    )

    assert proceed is True
    assert any("ollama-sidecar:11434" in url for url in seen["tags_urls"])
    assert any("ollama-sidecar:11434" in url for url in seen["pull_urls"])
    assert all("${" not in url for url in seen["tags_urls"])
    assert all("${" not in url for url in seen["pull_urls"])


@pytest.mark.unit
def test_environ_secret_values_filters_secret_keys() -> None:
    secrets = ollama_model._environ_secret_values(
        {"OLLAMA_API_KEY": "abcd1234", "PATH": "/usr/bin", "X": "yy"}
    )
    assert secrets == frozenset({"abcd1234"})


@pytest.mark.unit
async def test_non_opencode_runtime_is_noop(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_running(factory, agent="codex")
    executor = _make_executor(factory, tmp_path)

    def _must_not_run(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("ensure_ollama_model_available must not run for non-opencode")

    monkeypatch.setattr(ollama_model, "ensure_ollama_model_available", _must_not_run)

    proceed = await executor._ensure_ollama_model_or_mark_failed(
        workspace_id=workspace_id,
        ws=SimpleNamespace(agent="codex", task_policy={}),
    )

    assert proceed is True
    snap = await _get_status(factory, workspace_id)
    assert snap.status == WorkspaceStatus.running.value
