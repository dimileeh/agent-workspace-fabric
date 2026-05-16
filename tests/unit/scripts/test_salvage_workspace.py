"""Tests for ``scripts.salvage_workspace``.

Covers the no-op adapter factory (closure-capture regression guard
from CodeRabbit PR #2 feedback) and the ``_main`` CLI entry.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import base as _adapter_base
from awf.adapters import registry as _registry  # noqa: F401 - populates registry
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from scripts import salvage_workspace
from scripts.salvage_workspace import _install_noop_adapter_factory, _make_noop_factory
from tests.postgres import postgres_test_url


class TestClosureCapture:
    @pytest.mark.unit
    def test_each_factory_binds_its_own_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After installation, each registry entry must return an
        adapter whose ``.name`` matches ITS key. The closure-capture
        bug made every entry report the last-iterated runtime instead."""
        # Snapshot + restore registry so the test doesn't pollute.
        original_registry = dict(_adapter_base._REGISTRY)
        monkeypatch.setattr(_adapter_base, "_REGISTRY", dict(original_registry))

        _install_noop_adapter_factory()

        for registered_runtime, factory_cls in _adapter_base._REGISTRY.items():
            instance = factory_cls(runner=None, default_model=None)
            assert instance.name == registered_runtime, (
                f"factory for {registered_runtime.value} reports "
                f"{instance.name.value} — closure-capture regression"
            )

    @pytest.mark.unit
    def test_factory_builder_isolation(self) -> None:
        """``_make_noop_factory`` is the extraction that fixed the bug —
        each call must produce a class bound to the argument value, not
        to whatever state the caller's loop variable happens to hold
        later. Build two factories back-to-back, mutate nothing, and
        verify they keep their distinct runtimes."""
        codex_factory = _make_noop_factory(AgentRuntime.codex)
        claude_factory = _make_noop_factory(AgentRuntime.claude_code)

        assert codex_factory().name == AgentRuntime.codex
        assert claude_factory().name == AgentRuntime.claude_code

    @pytest.mark.unit
    def test_factory_accepts_full_adapter_constructor_surface(self) -> None:
        factory = _make_noop_factory(AgentRuntime.codex)

        adapter = factory(
            runner=None,
            default_model=None,
            default_effort=None,
            log_store=None,
            agent_wall_timeout_seconds=7200,
            agent_idle_timeout_seconds=3600,
        )

        assert adapter.name == AgentRuntime.codex

    @pytest.mark.unit
    async def test_factory_run_is_noop_with_success_message(self) -> None:
        """The adapter's ``run`` coroutine is what actually skips the
        agent. Must return exit 0 with the sentinel stdout so the
        executor logs reveal which run was salvaged."""
        factory = _make_noop_factory(AgentRuntime.codex)
        adapter = factory()
        result = await adapter.run(
            compose_project="awf_x",
            compose_file=Path("/tmp/compose.yml"),
            prompt="anything",
            model=None,
        )
        assert result.returncode == 0
        assert "skipping agent run" in result.stdout
        # _cli_args returns empty — this adapter never launches a CLI.
        assert adapter._cli_args(model=None) == []


# ── _main CLI ───────────────────────────────────────────────────────────────


class _FakeExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        transition_to: WorkspaceStatus = WorkspaceStatus.completed,
        **_: Any,
    ) -> None:
        self._factory = session_factory
        self._target = transition_to

    async def execute(self, workspace_id: str) -> None:
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            for t in (
                WorkspaceStatus.running,
                WorkspaceStatus.validating,
                WorkspaceStatus.pushing,
                WorkspaceStatus.monitoring_pr,
                self._target,
            ):
                if ws.status == t.value:
                    continue
                await repo.transition(ws, to=t, reason_code="FAKE_SALVAGE")
            await s.commit()


@pytest.fixture(autouse=True)
async def database_url(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    async with postgres_test_url() as url:
        monkeypatch.setenv("AWF_DATABASE_URL", url)
        yield url


async def _seed_salvage_workspace(*, initial_status: str) -> str:
    engine = make_engine(os.environ["AWF_DATABASE_URL"])
    factory = make_session_factory(engine)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="salvage me",
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requires_database=False,
        )
        for t in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
        ):
            await repo.transition(ws, to=t, reason_code="SEED")
        ws.branch_name = "awf/ws_salvage"
        ws.remote_push_branch = "awf/ws_salvage"
        ws.compose_project_name = "awf_ws_salvage"
        ws.base_commit = "d" * 40
        if initial_status != "ready":
            ws.status = initial_status  # bypass state machine
            if initial_status == "failed":
                ws.failure_reason = "agent_failure"
                ws.failure_message = "adapter killed"
        await s.commit()
        ws_id = ws.id
    await engine.dispose()
    return ws_id


class TestSalvageMain:
    @pytest.mark.unit
    async def test_happy_path_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_id = await _seed_salvage_workspace(initial_status="failed")
        original_registry = dict(_adapter_base._REGISTRY)

        def _exec_ctor(**kwargs: Any) -> _FakeExecutor:
            return _FakeExecutor(session_factory=kwargs["session_factory"])

        # Stub the heavy collaborators so we don't need docker/git subprocesses.
        monkeypatch.setattr(salvage_workspace, "WorkspaceExecutor", _exec_ctor)
        monkeypatch.setattr(salvage_workspace, "ComposeManager", lambda **_k: object())
        monkeypatch.setattr(salvage_workspace, "ValidationRunner", lambda **_k: object())
        monkeypatch.setattr(salvage_workspace, "PullRequestCreator", lambda *_a, **_k: object())
        monkeypatch.setattr(salvage_workspace, "AsyncioSubprocessRunner", lambda: object())

        try:
            rc = await salvage_workspace._main(tmp_path, ws_id)
            assert rc == 0
            assert original_registry == _adapter_base._REGISTRY
        finally:
            _adapter_base._REGISTRY.clear()
            _adapter_base._REGISTRY.update(original_registry)

    @pytest.mark.unit
    async def test_missing_db_returns_two(self, tmp_path: Path) -> None:
        rc = await salvage_workspace._main(tmp_path, "ws_whatever")
        assert rc == 2

    @pytest.mark.unit
    async def test_missing_workspace_returns_two(self, tmp_path: Path) -> None:
        await _seed_salvage_workspace(initial_status="ready")
        rc = await salvage_workspace._main(tmp_path, "ws_nonexistent")
        assert rc == 2

    @pytest.mark.unit
    async def test_already_ready_status_does_not_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the workspace is already ``ready`` the reset branch is
        skipped — the executor just picks it up."""
        ws_id = await _seed_salvage_workspace(initial_status="ready")
        monkeypatch.setattr(
            salvage_workspace,
            "WorkspaceExecutor",
            lambda **_k: _FakeExecutor(session_factory=_k["session_factory"]),
        )
        monkeypatch.setattr(salvage_workspace, "ComposeManager", lambda **_k: object())
        monkeypatch.setattr(salvage_workspace, "ValidationRunner", lambda **_k: object())
        monkeypatch.setattr(salvage_workspace, "PullRequestCreator", lambda *_a, **_k: object())
        monkeypatch.setattr(salvage_workspace, "AsyncioSubprocessRunner", lambda: object())
        rc = await salvage_workspace._main(tmp_path, ws_id)
        assert rc == 0

    @pytest.mark.unit
    async def test_executor_failed_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_id = await _seed_salvage_workspace(initial_status="failed")
        monkeypatch.setattr(
            salvage_workspace,
            "WorkspaceExecutor",
            lambda **_k: _FakeExecutor(
                session_factory=_k["session_factory"],
                transition_to=WorkspaceStatus.failed,
            ),
        )
        monkeypatch.setattr(salvage_workspace, "ComposeManager", lambda **_k: object())
        monkeypatch.setattr(salvage_workspace, "ValidationRunner", lambda **_k: object())
        monkeypatch.setattr(salvage_workspace, "PullRequestCreator", lambda *_a, **_k: object())
        monkeypatch.setattr(salvage_workspace, "AsyncioSubprocessRunner", lambda: object())
        rc = await salvage_workspace._main(tmp_path, ws_id)
        assert rc == 1


class TestNoOpAdapterDirect:
    @pytest.mark.unit
    async def test_noop_adapter_returns_canned_stdout(self) -> None:
        """Original ``_NoOpAdapter`` is retained (not strictly used by
        ``_install_noop_adapter_factory`` which uses the
        ``_make_noop_factory`` path) — cover it directly so a future
        refactor doesn't silently remove a public API."""
        adapter = salvage_workspace._NoOpAdapter(runtime=AgentRuntime.codex)
        assert adapter.name == AgentRuntime.codex
        assert adapter._cli_args(model=None) == []
        result = await adapter.run(
            compose_project="p",
            compose_file=Path("/tmp/c.yml"),
            prompt="anything",
        )
        assert result.returncode == 0
        assert "skipping agent run" in result.stdout
