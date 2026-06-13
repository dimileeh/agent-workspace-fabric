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
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
    ollama_model,
)
from awf.db.enums import WorkspaceStatus
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
) -> WorkspaceExecutor:
    fake = FakeCommandRunner()
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=SimpleNamespace(),
        validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
        pr_creator=PullRequestCreator(fake),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
        ),
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
