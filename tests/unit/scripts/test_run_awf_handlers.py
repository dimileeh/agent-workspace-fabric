"""Integration tests for ``scripts.run_awf``'s three task handlers.

Covers the inline ``feature_branch_pr`` branch of ``_run_task`` plus
``_run_sync_release_pr`` and ``_run_sync_feature_pr``. Each handler
touches ~150 lines of provisioning + compose + monitor wiring; this
file drives each end-to-end with fake collaborators so we can assert
on the durable side effects (workspace row state, final transitions,
the ``remote_push_branch`` column).

The 2026-04-23 push-misdirection incident is the regression shield.
Each handler must:

  * Persist ``branch_name`` and ``remote_push_branch`` correctly.
    For ``feature_branch_pr``: both equal ``awf/<id>``.
    For ``sync_release_pr``: ``branch_name`` is ``release-sync/<id>``,
    ``remote_push_branch`` is the PR's head (``source_branch``).
    For ``sync_feature_pr``: ``branch_name`` is ``feature-sync/<id>``,
    ``remote_push_branch`` is the PR's head (``source_branch``).
  * Walk the state machine to a known terminal state set by the fake
    executor / monitor.

The collaborators we fake (GitManager, ComposeManager, WorkspaceExecutor,
release/feature PR monitors, AsyncioSubprocessRunner) are all tested
directly in their own unit-test files — this file cares about the
*handler's* own logic (DB writes, transition sequencing,
remote_push_branch correctness)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from scripts import run_awf

# ── Fakes ──────────────────────────────────────────────────────────────────


@dataclass
class _FakeWorktreeLayout:
    worktree_path: Path
    mirror_path: Path


@dataclass
class _FakeGitManager:
    work_dir: Path
    added: list[dict[str, Any]] = field(default_factory=list)

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.added = []

    async def add_worktree(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        base_branch: str,
        new_branch: str,
    ) -> _FakeWorktreeLayout:
        self.added.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "base_branch": base_branch,
                "new_branch": new_branch,
            }
        )
        worktree = self.work_dir / "worktrees" / workspace_id
        mirror = self.work_dir / "mirrors" / f"{workspace_id}.git"
        worktree.mkdir(parents=True, exist_ok=True)
        mirror.mkdir(parents=True, exist_ok=True)
        return _FakeWorktreeLayout(worktree_path=worktree, mirror_path=mirror)

    async def head_sha(self, *, workspace_id: str) -> str:
        return "a" * 40

    async def remove_worktree(self, *, workspace_id: str, repo_url: str) -> None:
        pass


@dataclass
class _FakeComposeManager:
    ups: list[Any] = field(default_factory=list)

    def __init__(self, *, work_dir: Path, template_path: Path) -> None:
        self.ups = []

    async def up(self, spec: Any, *, wait: bool = True) -> None:
        self.ups.append(spec)


class _FakeExecutor:
    """Drives the workspace from ``ready`` to ``completed`` with a
    canned PR URL. Mirrors ``awf.control.executor.WorkspaceExecutor``'s
    external surface (an ``execute(workspace_id)`` coroutine).

    Also invokes ``pr_monitor_factory`` once during ``execute`` so the
    handler's factory closure runs — without this, the closure body is
    defined but never called, and coverage misses the line."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        pr_url: str = "https://github.com/dimileeh/aira-web/pull/111",
        pr_monitor_factory: Any = None,
        **_kwargs: Any,
    ) -> None:
        self._factory = session_factory
        self._pr_url = pr_url
        self._monitor_factory = pr_monitor_factory
        self.calls: list[str] = []

    async def execute(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)
        if self._monitor_factory is not None:
            # Exercise the factory closure so its body is covered.
            # Pass a dummy adapter sentinel; the fake monitor ignores it.
            self._monitor_factory(object())
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            for target in (
                WorkspaceStatus.running,
                WorkspaceStatus.validating,
                WorkspaceStatus.pushing,
                WorkspaceStatus.monitoring_pr,
                WorkspaceStatus.completed,
            ):
                await repo.transition(ws, to=target, reason_code="TEST")
            ws.pr_url = self._pr_url
            await s.commit()


class _FakeMonitor:
    """Drives a monitoring_pr workspace to completed."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self.calls: list[dict[str, Any]] = []

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "compose_project": compose_project,
                "compose_file": str(compose_file),
            }
        )
        async with self._factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="MON_DONE")
            await s.commit()


class _FakeRunner:
    """Stand-in for AsyncioSubprocessRunner — the handlers pass it to
    collaborators we've already faked, so its methods are never actually
    called. It just needs to exist and be constructible."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def run(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("FakeRunner.run should not be called in these tests")


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'handlers.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_workspace_db(db_path: Path, workspace_id: str = "ws_existing") -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    status=WorkspaceStatus.completed.value,
                    repo_url="git@github.com:x/y.git",
                    branch_base="main",
                    task_title="existing workspace",
                    task_prompt="keep me",
                    agent="codex",
                    test_commands=[],
                    requires_database=False,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _workspace_exists(db_path: Path, workspace_id: str = "ws_existing") -> bool:
    engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            return await session.get(Workspace, workspace_id) is not None
    finally:
        await engine.dispose()


@pytest.fixture
def fake_gitmanager_cls(tmp_path: Path) -> Callable[..., _FakeGitManager]:
    """Returns a class-like factory that also exposes the single instance
    the handler constructs (for assertions). Needed because the handler
    does ``git = GitManager(work_dir / "git")`` and we want to inspect
    what was added."""
    instances: list[_FakeGitManager] = []

    def _factory(git_dir: Path) -> _FakeGitManager:
        inst = _FakeGitManager(git_dir)
        instances.append(inst)
        return inst

    _factory.instances = instances  # type: ignore[attr-defined]
    return _factory


@pytest.fixture
def patch_handlers(
    monkeypatch: pytest.MonkeyPatch,
    fake_gitmanager_cls: Callable[..., _FakeGitManager],
) -> dict[str, Any]:
    """Swap every heavy-I/O collaborator in ``scripts.run_awf`` with a
    fake. Returns a dict of hooks the test can inspect."""
    monkeypatch.setattr(run_awf, "GitManager", fake_gitmanager_cls)
    compose_instances: list[_FakeComposeManager] = []

    def _compose_ctor(*, work_dir: Path, template_path: Path) -> _FakeComposeManager:
        inst = _FakeComposeManager(work_dir=work_dir, template_path=template_path)
        compose_instances.append(inst)
        return inst

    monkeypatch.setattr(run_awf, "ComposeManager", _compose_ctor)
    monkeypatch.setattr(run_awf, "AsyncioSubprocessRunner", _FakeRunner)

    executors: list[_FakeExecutor] = []
    monitors: list[_FakeMonitor] = []
    monitor_builder_calls: list[dict[str, Any]] = []

    def _exec_ctor(**kwargs: Any) -> _FakeExecutor:
        e = _FakeExecutor(
            session_factory=kwargs["session_factory"],
            pr_monitor_factory=kwargs.get("pr_monitor_factory"),
        )
        executors.append(e)
        return e

    monkeypatch.setattr(run_awf, "WorkspaceExecutor", _exec_ctor)

    def _build_release_monitor(**kwargs: Any) -> _FakeMonitor:
        monitor_builder_calls.append({"kind": "release", "kwargs": kwargs})
        m = _FakeMonitor(session_factory=kwargs["session_factory"])
        monitors.append(m)
        return m

    def _build_feature_monitor(**kwargs: Any) -> _FakeMonitor:
        monitor_builder_calls.append({"kind": "feature", "kwargs": kwargs})
        m = _FakeMonitor(session_factory=kwargs["session_factory"])
        monitors.append(m)
        return m

    # Patch at the source module because the sync handlers import these
    # lazily inside their own function bodies — patching on ``run_awf``
    # alone only catches the feature_branch_pr path's import.
    monkeypatch.setattr(
        "awf.runtime.release_pr_monitor.build_release_pr_monitor",
        _build_release_monitor,
    )
    monkeypatch.setattr(
        "awf.runtime.release_pr_monitor.build_feature_pr_monitor",
        _build_feature_monitor,
    )
    monkeypatch.setattr(run_awf, "build_feature_pr_monitor", _build_feature_monitor)
    monkeypatch.setattr(run_awf, "build_release_pr_monitor", _build_release_monitor)

    # ValidationRunner + PullRequestCreator + GitHubClient get constructed
    # but their methods aren't exercised because the fake executor never
    # calls them. Leave the real classes in place.

    # _configure_branch_push_upstream runs git-config via the fake runner;
    # the FakeRunner.run raises on call, so short-circuit it.
    async def _noop_config(
        *, runner: Any, worktree_path: Any, branch_name: str, remote_branch: str
    ) -> None:
        pass

    monkeypatch.setattr(run_awf, "_configure_branch_push_upstream", _noop_config)

    return {
        "git_factory": fake_gitmanager_cls,
        "executors": executors,
        "monitors": monitors,
        "monitor_builder_calls": monitor_builder_calls,
        "compose_instances": compose_instances,
    }


def _cfg(task_kind: str = "feature_branch_pr", **overrides: Any) -> run_awf.TaskConfig:
    from dataclasses import replace

    base = run_awf.TaskConfig(
        repo_url="git@github.com:dimileeh/aira-web.git",
        branch_base="development",
        task_title="handler test",
        task_prompt="do the thing",
        agent="codex",
        test_commands=[],
        task_kind=task_kind,
    )
    return replace(base, **overrides) if overrides else base


# ── feature_branch_pr handler ──────────────────────────────────────────────


class TestFeatureBranchPrHandler:
    @pytest.mark.unit
    async def test_happy_path_sets_remote_push_branch_to_feature_branch(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        result = await run_awf._run_task(
            _cfg(),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="tester",
            git_email="t@example.com",
        )

        ws_id = result["workspace_id"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.branch_name == f"awf/{ws_id}"
            # The T39 regression shield: feature_branch_pr workspaces
            # MUST have remote_push_branch == branch_name. Anything else
            # means the monitor's explicit-refspec push would target the
            # wrong ref on GitHub.
            assert ws.remote_push_branch == ws.branch_name, (
                "feature_branch_pr handler must set "
                "remote_push_branch = branch_name so the monitor pushes "
                "to origin/awf/<id>, not to development or some other "
                "auto-tracked branch (2026-04-23 incident)"
            )
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.base_commit == "a" * 40
            assert ws.compose_project_name == f"awf_{ws_id}"
            assert ws.compose_file_path == str(
                tmp_path / "compose" / "compose" / ws_id / "compose.yml"
            )

        # The FakeExecutor was constructed exactly once and drove one
        # workspace to completion.
        execs = patch_handlers["executors"]
        assert len(execs) == 1
        assert execs[0].calls == [ws_id]

    @pytest.mark.unit
    async def test_auto_merge_false_routes_feature_branch_pr_to_manual_monitor(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        await run_awf._run_task(
            _cfg(auto_merge=False),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="tester",
            git_email="t@example.com",
        )
        assert patch_handlers["monitor_builder_calls"][0]["kind"] == "release"

    @pytest.mark.unit
    async def test_task_grace_override_is_passed_to_feature_pr_monitor(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        await run_awf._run_task(
            _cfg(initial_review_grace_period_seconds=0),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="tester",
            git_email="t@example.com",
        )
        kwargs = patch_handlers["monitor_builder_calls"][0]["kwargs"]
        assert kwargs["initial_review_grace_period_seconds"] == 0

    @pytest.mark.unit
    async def test_profile_grace_is_passed_when_task_omits_override(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        await run_awf._run_task(
            _cfg(
                profile={
                    "name": "custom",
                    "monitor": {"initial_review_grace_period_seconds": 321},
                },
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="tester",
            git_email="t@example.com",
        )
        kwargs = patch_handlers["monitor_builder_calls"][0]["kwargs"]
        assert kwargs["initial_review_grace_period_seconds"] == 321

    @pytest.mark.unit
    async def test_companions_get_materialized_before_compose_up(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Companions listed in cfg.companions must each be materialized
        as a CompanionService before compose.up, with ${POSTGRES_URL}
        placeholders resolved."""
        comp = {
            "name": "backend",
            "repo_url": "git@github.com:dimileeh/aira-agent.git",
            "branch": "development",
            "environment": {
                "DB_URL": "${POSTGRES_URL}",
                "STATIC": "nope",
            },
        }
        result = await run_awf._run_task(
            _cfg(companions=[comp]),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        ws_id = result["workspace_id"]

        # The materializer was called on the companion — check via the
        # fake GitManager, which logs every add_worktree. There should
        # be 2 adds: the main workspace + the backend companion.
        instances = patch_handlers["git_factory"].instances
        assert len(instances) == 1
        adds = instances[0].added
        assert len(adds) == 2
        main_add = adds[0]
        companion_add = adds[1]
        assert main_add["new_branch"] == f"awf/{ws_id}"
        assert "backend" in companion_add["new_branch"]
        assert ws_id in companion_add["new_branch"]  # owner-scoped path fix
        # Legacy companion specs that depend on postgres now get an explicit
        # profile service rather than relying on the base template.
        compose_spec = patch_handlers["compose_instances"][0].ups[0]
        assert any(s.name == "postgres" for s in compose_spec.services)

    @pytest.mark.unit
    async def test_result_shape_contains_contract_fields(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        result = await run_awf._run_task(
            _cfg(),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        assert set(result.keys()) >= {
            "workspace_id",
            "title",
            "status",
            "pr_url",
            "failure_reason",
            "failure_message",
            "branch",
            "base_commit",
        }
        assert result["title"] == "handler test"
        assert result["status"] == WorkspaceStatus.completed.value
        assert result["pr_url"] == "https://github.com/dimileeh/aira-web/pull/111"


# ── sync_release_pr handler ────────────────────────────────────────────────


class TestSyncReleasePrHandler:
    @pytest.mark.unit
    async def test_remote_push_branch_is_source_branch_not_local(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """The 2026-04-23 regression shield for sync workspaces.

        The local branch is ``release-sync/<id>`` (per-workspace ref
        for race avoidance). The REMOTE target is the PR's head,
        typically ``development``. Mixing these up would recreate the
        incident — the monitor would push HEAD to ``release-sync/<id>``
        on origin instead of updating ``development``."""
        result = await run_awf._run_task(
            _cfg(
                task_kind="sync_release_pr",
                branch_base="main",
                source_branch="development",
                pr_number=278,
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        ws_id = result["workspace_id"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.task_kind == "sync_release_pr"
            assert ws.branch_name == f"release-sync/{ws_id}"
            assert ws.remote_push_branch == "development", (
                "sync_release_pr must push to source_branch, not local ref"
            )
            assert ws.pr_number == 278
            assert ws.pr_url == "https://github.com/dimileeh/aira-web/pull/278"
        kwargs = patch_handlers["monitor_builder_calls"][0]["kwargs"]
        assert isinstance(kwargs["log_store"], run_awf.LogStore)

    @pytest.mark.unit
    async def test_missing_pr_number_raises(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        with pytest.raises(ValueError, match="requires cfg.pr_number"):
            await run_awf._run_task(
                _cfg(task_kind="sync_release_pr", branch_base="main"),
                work_dir=tmp_path,
                session_factory=factory,
                auth_mounts=[],
                git_name="t",
                git_email="t@e.com",
            )

    @pytest.mark.unit
    async def test_source_branch_defaults_to_development(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """When cfg.source_branch is None, it defaults to development."""
        result = await run_awf._run_task(
            _cfg(task_kind="sync_release_pr", branch_base="main", pr_number=300),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        ws_id = result["workspace_id"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.remote_push_branch == "development"

    @pytest.mark.unit
    async def test_companions_are_materialized_with_postgres_url_expanded(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Sync-release workspaces often run with a postgres companion;
        its ``${POSTGRES_URL}`` env placeholder must be expanded the
        same way the feature-branch handler does it."""
        comp = {
            "name": "aira-backend",
            "repo_url": "git@github.com:dimileeh/aira-agent.git",
            "branch": "development",
            "environment": {"AIRA_DATABASE_URL": "${POSTGRES_URL}"},
        }
        result = await run_awf._run_task(
            _cfg(
                task_kind="sync_release_pr",
                branch_base="main",
                source_branch="development",
                pr_number=300,
                companions=[comp],
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        ws_id = result["workspace_id"]
        # 2 add_worktree calls: release-sync branch + backend companion.
        instances = patch_handlers["git_factory"].instances
        adds = instances[0].added
        assert len(adds) == 2
        # Companion's worktree scope includes the owning workspace id.
        assert ws_id in adds[1]["new_branch"]


# ── sync_feature_pr handler ────────────────────────────────────────────────


class TestSyncFeaturePrHandler:
    @pytest.mark.unit
    async def test_remote_push_branch_is_pr_head_branch(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        result = await run_awf._run_task(
            _cfg(
                task_kind="sync_feature_pr",
                branch_base="development",
                source_branch="fix/sprints-guard",
                pr_number=277,
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        ws_id = result["workspace_id"]
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.task_kind == "sync_feature_pr"
            assert ws.branch_name == f"feature-sync/{ws_id}"
            assert ws.remote_push_branch == "fix/sprints-guard", (
                "sync_feature_pr must push to the PR's head branch"
            )
            assert ws.pr_number == 277
        kwargs = patch_handlers["monitor_builder_calls"][0]["kwargs"]
        assert isinstance(kwargs["log_store"], run_awf.LogStore)

    @pytest.mark.unit
    async def test_missing_source_branch_raises(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Feature-sync has no ``development`` fallback — a missing head
        branch is a programming error upstream."""
        with pytest.raises(ValueError, match="source_branch"):
            await run_awf._run_task(
                _cfg(
                    task_kind="sync_feature_pr",
                    branch_base="development",
                    pr_number=277,
                ),
                work_dir=tmp_path,
                session_factory=factory,
                auth_mounts=[],
                git_name="t",
                git_email="t@e.com",
            )

    @pytest.mark.unit
    async def test_default_auto_merge_true_routes_to_feature_monitor(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """AWF's contract: a green feature→development PR lands
        automatically. ``TaskConfig.auto_merge`` defaults to ``True`` so
        the handler routes to ``build_feature_pr_monitor`` (hardcodes
        ``auto_merge=True``) instead of ``build_release_pr_monitor``
        (hardcodes ``auto_merge=False`` → NotifyHuman). Regression shield
        for PR #277, which got stuck at "ready to merge" because the
        default was ``False``."""
        which_builder_ran: list[str] = []

        class _TinyMonitor:
            def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
                self._factory = session_factory

            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                async with self._factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="T")
                    await s.commit()

        def _tiny_release(**kwargs: Any) -> _TinyMonitor:
            which_builder_ran.append("release")
            return _TinyMonitor(session_factory=kwargs["session_factory"])

        def _tiny_feature(**kwargs: Any) -> _TinyMonitor:
            which_builder_ran.append("feature")
            return _TinyMonitor(session_factory=kwargs["session_factory"])

        monkeypatch.setattr(
            "awf.runtime.release_pr_monitor.build_release_pr_monitor", _tiny_release
        )
        monkeypatch.setattr(
            "awf.runtime.release_pr_monitor.build_feature_pr_monitor", _tiny_feature
        )
        monkeypatch.setattr(run_awf, "build_feature_pr_monitor", _tiny_feature)
        monkeypatch.setattr(run_awf, "build_release_pr_monitor", _tiny_release)
        monkeypatch.setattr(run_awf, "GitManager", lambda p: _FakeGitManager(p))
        monkeypatch.setattr(run_awf, "ComposeManager", _FakeComposeManager)
        monkeypatch.setattr(run_awf, "AsyncioSubprocessRunner", _FakeRunner)

        async def _noop_config(**_kw: Any) -> None:
            pass

        monkeypatch.setattr(run_awf, "_configure_branch_push_upstream", _noop_config)

        # Default auto_merge — explicitly NOT set on TaskConfig.
        await run_awf._run_task(
            _cfg(
                task_kind="sync_feature_pr",
                branch_base="development",
                source_branch="fix/some-head",
                pr_number=888,
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        assert which_builder_ran == ["feature"], (
            "default auto_merge must route to build_feature_pr_monitor "
            "(which hardcodes auto_merge=True). Anything else reopens "
            "the PR #277 bug where feature→dev PRs got stuck as "
            "NotifyHuman forever."
        )

    @pytest.mark.unit
    async def test_explicit_auto_merge_false_routes_to_release_monitor(
        self,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Opt-out still works: ``auto_merge=False`` routes to the
        release monitor (notify-human terminal). Needed for one-off
        recovery invocations."""
        which_builder_ran: list[str] = []

        class _TinyMonitor:
            def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
                self._factory = session_factory

            async def run(
                self, *, workspace_id: str, compose_project: str, compose_file: Path
            ) -> None:
                async with self._factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="T")
                    await s.commit()

        def _tiny_release(**kwargs: Any) -> _TinyMonitor:
            which_builder_ran.append("release")
            return _TinyMonitor(session_factory=kwargs["session_factory"])

        def _tiny_feature(**kwargs: Any) -> _TinyMonitor:
            which_builder_ran.append("feature")
            return _TinyMonitor(session_factory=kwargs["session_factory"])

        monkeypatch.setattr(
            "awf.runtime.release_pr_monitor.build_release_pr_monitor", _tiny_release
        )
        monkeypatch.setattr(
            "awf.runtime.release_pr_monitor.build_feature_pr_monitor", _tiny_feature
        )
        monkeypatch.setattr(run_awf, "build_feature_pr_monitor", _tiny_feature)
        monkeypatch.setattr(run_awf, "build_release_pr_monitor", _tiny_release)
        monkeypatch.setattr(run_awf, "GitManager", lambda p: _FakeGitManager(p))
        monkeypatch.setattr(run_awf, "ComposeManager", _FakeComposeManager)
        monkeypatch.setattr(run_awf, "AsyncioSubprocessRunner", _FakeRunner)

        async def _noop_config(**_kw: Any) -> None:
            pass

        monkeypatch.setattr(run_awf, "_configure_branch_push_upstream", _noop_config)

        await run_awf._run_task(
            _cfg(
                task_kind="sync_feature_pr",
                branch_base="development",
                source_branch="fix/some-head",
                pr_number=777,
                auto_merge=False,
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        assert which_builder_ran == ["release"]

    @pytest.mark.unit
    async def test_missing_pr_number_raises(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        with pytest.raises(ValueError, match="requires cfg.pr_number"):
            await run_awf._run_task(
                _cfg(
                    task_kind="sync_feature_pr",
                    branch_base="development",
                    source_branch="fix/foo",
                ),
                work_dir=tmp_path,
                session_factory=factory,
                auth_mounts=[],
                git_name="t",
                git_email="t@e.com",
            )

    @pytest.mark.unit
    async def test_companions_materialized_for_sync_feature(
        self,
        factory: async_sessionmaker[AsyncSession],
        patch_handlers: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        comp = {
            "name": "aira-backend",
            "repo_url": "git@github.com:dimileeh/aira-agent.git",
            "branch": "development",
            "environment": {"AIRA_DATABASE_URL": "${POSTGRES_URL}"},
        }
        result = await run_awf._run_task(
            _cfg(
                task_kind="sync_feature_pr",
                branch_base="development",
                source_branch="fix/some-pr-head",
                pr_number=123,
                companions=[comp],
            ),
            work_dir=tmp_path,
            session_factory=factory,
            auth_mounts=[],
            git_name="t",
            git_email="t@e.com",
        )
        ws_id = result["workspace_id"]
        instances = patch_handlers["git_factory"].instances
        adds = instances[0].added
        assert len(adds) == 2
        assert ws_id in adds[1]["new_branch"]


# ── _build_auth_mounts ─────────────────────────────────────────────────────


class TestMainEntry:
    @pytest.mark.unit
    async def test_main_drives_all_tasks_and_returns_zero_on_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_main`` reads a JSON config, fans out via asyncio.gather,
        prints the summary, and returns 0 when every task completes.
        Monkey-patched ``_run_task_with_failure_guard`` so we don't
        drive real provisioning; we only care the orchestrator reads
        the config and aggregates results correctly."""
        config = tmp_path / "tasks.json"
        config.write_text(
            '[{"repo_url": "git@github.com:x/y.git", "branch_base": "development", '
            '"task_title": "t1", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}, '
            '{"repo_url": "git@github.com:x/y.git", "branch_base": "development", '
            '"task_title": "t2", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}]'
        )

        captured_titles: list[str] = []

        async def _fake_run_task_with_guard(cfg, **kwargs):  # type: ignore[no-untyped-def]
            captured_titles.append(cfg.task_title)
            return {
                "workspace_id": f"ws_{cfg.task_title}",
                "title": cfg.task_title,
                "status": "completed",
                "pr_url": f"https://example/pr/{cfg.task_title}",
                "failure_reason": None,
                "failure_message": None,
                "branch": "awf/x",
                "base_commit": "a" * 40,
            }

        monkeypatch.setattr(run_awf, "_run_task_with_failure_guard", _fake_run_task_with_guard)
        # Isolate HOME so real .codex / .claude dirs aren't mounted.
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        rc = await run_awf._main(
            config_path=config,
            work_dir=tmp_path / "work",
            keep_state=True,
        )
        assert rc == 0
        assert captured_titles == ["t1", "t2"]

    @pytest.mark.unit
    async def test_main_returns_one_if_any_task_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = tmp_path / "tasks.json"
        config.write_text(
            '[{"repo_url": "git@github.com:x/y.git", "branch_base": "development", '
            '"task_title": "ok", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}, '
            '{"repo_url": "git@github.com:x/y.git", "branch_base": "development", '
            '"task_title": "bad", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}]'
        )

        async def _fake_run_task_with_guard(cfg, **kwargs):  # type: ignore[no-untyped-def]
            if cfg.task_title == "bad":
                return {
                    "workspace_id": "ws_bad",
                    "title": "bad",
                    "status": "failed",
                    "pr_url": None,
                    "failure_reason": "validation_failure",
                    "failure_message": "pytest failed",
                    "branch": "awf/bad",
                    "base_commit": "a" * 40,
                }
            return {
                "workspace_id": "ws_ok",
                "title": "ok",
                "status": "completed",
                "pr_url": "https://example/pr/ok",
                "failure_reason": None,
                "failure_message": None,
                "branch": "awf/ok",
                "base_commit": "a" * 40,
            }

        monkeypatch.setattr(run_awf, "_run_task_with_failure_guard", _fake_run_task_with_guard)
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        rc = await run_awf._main(
            config_path=config,
            work_dir=tmp_path / "work",
            keep_state=True,
        )
        assert rc == 1

    @pytest.mark.unit
    async def test_main_returns_one_when_a_task_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A task that raises (rather than failing cleanly) must still
        make ``_main`` exit 1 — return_exceptions=True on gather stops
        one blow-up from crashing the whole driver, but the summary
        must mark it as failed."""
        config = tmp_path / "tasks.json"
        config.write_text(
            '[{"repo_url": "git@github.com:x/y.git", "branch_base": "development", '
            '"task_title": "boom", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}]'
        )

        async def _fake_run_task_with_guard(cfg, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("unexpected collapse")

        monkeypatch.setattr(run_awf, "_run_task_with_failure_guard", _fake_run_task_with_guard)
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        rc = await run_awf._main(
            config_path=config,
            work_dir=tmp_path / "work",
            keep_state=True,
        )
        assert rc == 1

    @pytest.mark.unit
    async def test_main_preserves_existing_db_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reused work dir must not silently replace ``awf.db``.

        The API process may have this SQLite file open while an operator
        launches another ``run_awf.py`` against the same work dir. If the
        launcher unlinks the file, the API keeps serving the old anonymous
        inode while the new run writes to a fresh DB path.
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        db_path = work_dir / "awf.db"
        await _seed_workspace_db(db_path)

        config = tmp_path / "tasks.json"
        config.write_text(
            '[{"repo_url": "git@github.com:x/y.git", "branch_base": "main", '
            '"task_title": "new task", "task_prompt": "p", "agent": "codex", '
            '"test_commands": [], "requires_database": false}]'
        )
        saw_existing_row: list[bool] = []

        async def _fake_run_task_with_guard(cfg, **kwargs):  # type: ignore[no-untyped-def]
            async with kwargs["session_factory"]() as session:
                saw_existing_row.append(await session.get(Workspace, "ws_existing") is not None)
            return {
                "workspace_id": "ws_new",
                "title": cfg.task_title,
                "status": "completed",
                "pr_url": "https://example/pr/new",
                "failure_reason": None,
                "failure_message": None,
                "branch": "awf/new",
                "base_commit": "a" * 40,
            }

        monkeypatch.setattr(
            run_awf,
            "_run_task_with_failure_guard",
            _fake_run_task_with_guard,
        )
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        await run_awf._main(config_path=config, work_dir=work_dir, keep_state=False)

        assert saw_existing_row == [True]
        assert await _workspace_exists(db_path)

    @pytest.mark.unit
    async def test_main_resets_db_only_when_explicitly_requested(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        db_path = work_dir / "awf.db"
        await _seed_workspace_db(db_path)

        config = tmp_path / "tasks.json"
        config.write_text("[]")

        async def _noop(cfg, **kwargs):  # type: ignore[no-untyped-def]
            return {}

        monkeypatch.setattr(run_awf, "_run_task_with_failure_guard", _noop)
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        await run_awf._main(
            config_path=config,
            work_dir=work_dir,
            keep_state=False,
            reset_state=True,
        )

        assert not await _workspace_exists(db_path)


class TestBuildAuthMounts:
    @pytest.mark.unit
    def test_builds_mounts_for_existing_paths_only(self, tmp_path: Path) -> None:
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".claude").mkdir()
        # .claude.json exists as a file
        (tmp_path / ".claude.json").write_text("{}")
        (tmp_path / ".config" / "opencode").mkdir(parents=True)
        (tmp_path / ".ollama").mkdir()
        (tmp_path / ".config" / "gh").mkdir(parents=True)
        (tmp_path / ".config" / "gcloud").mkdir(parents=True)
        (tmp_path / ".gitconfig").write_text("")
        # .gemini and .ssh are missing on purpose
        mounts = run_awf._build_auth_mounts(tmp_path)
        targets = [m.target for m in mounts]
        assert "/home/agent/.codex" not in targets
        assert "/home/agent/.claude" in targets
        assert "/home/agent/.claude.json" in targets
        assert "/home/agent/.config/gh" in targets
        assert "/home/agent/.config/gcloud" in targets
        assert "/home/agent/.gitconfig" in targets
        assert "/home/agent/.config/opencode" not in targets
        assert "/home/agent/.ollama" not in targets
        assert "/home/agent/.gemini" not in targets
        assert "/home/agent/.ssh" not in targets

    @pytest.mark.unit
    def test_rw_vs_ro_mount_modes(self, tmp_path: Path) -> None:
        """Credentials that need in-session writes (claude/gemini
        caches) must be rw; stable creds (gh, gitconfig, ssh) must be
        ro. The handler's first principle is not letting task runs
        pollute the operator's home — so the ro list is the important
        assertion."""
        for d in (".codex", ".claude", ".gemini"):
            (tmp_path / d).mkdir()
        (tmp_path / ".config" / "opencode").mkdir(parents=True)
        (tmp_path / ".ollama").mkdir()
        (tmp_path / ".claude.json").write_text("{}")
        (tmp_path / ".config" / "gh").mkdir(parents=True)
        (tmp_path / ".config" / "gcloud").mkdir(parents=True)
        (tmp_path / ".gitconfig").write_text("")
        (tmp_path / ".ssh").mkdir()

        mounts = run_awf._build_auth_mounts(tmp_path)
        by_target = {m.target: m for m in mounts}
        assert "/home/agent/.codex" not in by_target
        assert "/home/agent/.config/opencode" not in by_target
        assert "/home/agent/.ollama" not in by_target
        assert by_target["/home/agent/.claude"].mode == "rw"
        assert by_target["/home/agent/.gemini"].mode == "rw"
        assert by_target["/home/agent/.config/gh"].mode == "ro"
        assert by_target["/home/agent/.config/gcloud"].mode == "ro"
        assert by_target["/home/agent/.gitconfig"].mode == "ro"
        assert by_target["/home/agent/.ssh"].mode == "ro"

    @pytest.mark.unit
    def test_google_application_credentials_file_is_mounted(self, tmp_path: Path) -> None:
        credentials = tmp_path / "svc.json"
        credentials.write_text("{}")

        mounts = run_awf._build_auth_mounts(
            tmp_path,
            host_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials)},
        )

        by_target = {m.target: m for m in mounts}
        assert by_target[str(credentials)].source == str(credentials)
        assert by_target[str(credentials)].mode == "ro"

    @pytest.mark.unit
    def test_missing_home_returns_empty(self, tmp_path: Path) -> None:
        # Point at a dir with nothing in it.
        empty = tmp_path / "nothing-here"
        empty.mkdir()
        mounts = run_awf._build_auth_mounts(empty)
        assert mounts == []

    @pytest.mark.unit
    def test_workspace_auth_mounts_seed_isolated_codex_home(self, tmp_path: Path) -> None:
        host_home = tmp_path / "host"
        host_codex = host_home / ".codex"
        host_codex.mkdir(parents=True)
        (host_codex / "auth.json").write_text('{"token": "redacted"}')
        (host_codex / "config.toml").write_text("model = 'gpt-5.5'\n")
        (host_codex / "installation_id").write_text("install-123")
        (host_codex / "logs_2.sqlite").write_text("do not copy")
        (host_codex / "sessions").mkdir()
        (host_codex / "rules").mkdir()
        (host_codex / "rules" / "default.rules").write_text("rule")
        base_mount = run_awf.AuthMount(
            source=str(host_home / ".claude"),
            target="/home/agent/.claude",
            mode="rw",
        )

        mounts = run_awf._workspace_auth_mounts(
            [base_mount],
            workspace_id="ws_test",
            work_dir=tmp_path / "work",
            host_home=host_home,
        )

        codex_mount = mounts[0]
        codex_home = Path(codex_mount.source)
        assert codex_mount.target == "/home/agent/.codex"
        assert codex_mount.mode == "rw"
        assert codex_home == tmp_path / "work" / "auth" / "ws_test" / "codex"
        assert (codex_home / "auth.json").read_text() == '{"token": "redacted"}'
        assert (codex_home / "config.toml").read_text() == "model = 'gpt-5.5'\n"
        assert (codex_home / "installation_id").read_text() == "install-123"
        assert (codex_home / "rules" / "default.rules").read_text() == "rule"
        assert not (codex_home / "logs_2.sqlite").exists()
        assert not (codex_home / "sessions").exists()
        assert mounts[1] == base_mount

    @pytest.mark.unit
    def test_workspace_auth_mounts_skip_codex_when_missing(self, tmp_path: Path) -> None:
        host_home = tmp_path / "host"
        host_home.mkdir()
        base_mount = run_awf.AuthMount(
            source=str(host_home / ".claude"),
            target="/home/agent/.claude",
            mode="rw",
        )

        mounts = run_awf._workspace_auth_mounts(
            [base_mount],
            workspace_id="ws_test",
            work_dir=tmp_path / "work",
            host_home=host_home,
        )

        assert mounts == (base_mount,)

    @pytest.mark.unit
    def test_workspace_auth_mounts_seed_isolated_opencode_and_ollama_auth(
        self,
        tmp_path: Path,
    ) -> None:
        host_home = tmp_path / "host"
        host_opencode = host_home / ".config" / "opencode"
        host_ollama = host_home / ".ollama"
        host_opencode.mkdir(parents=True)
        host_ollama.mkdir(parents=True)
        (host_opencode / "opencode.json").write_text('{"model": "initial"}\n')
        (host_ollama / "config.json").write_text('{"integrations": {}}\n')
        (host_ollama / "id_ed25519").write_text("private-key\n")
        (host_ollama / "models").mkdir()
        (host_ollama / "models" / "large-blob").write_text("do not copy\n")
        base_mount = run_awf.AuthMount(
            source=str(host_home / ".claude"),
            target="/home/agent/.claude",
            mode="rw",
        )

        mounts = run_awf._workspace_auth_mounts(
            [base_mount],
            workspace_id="ws_test",
            work_dir=tmp_path / "work",
            host_home=host_home,
        )

        by_target = {m.target: m for m in mounts}
        opencode_mount = by_target["/home/agent/.config/opencode"]
        ollama_mount = by_target["/home/agent/.ollama"]
        opencode_home = Path(opencode_mount.source)
        ollama_home = Path(ollama_mount.source)
        assert opencode_mount.mode == "rw"
        assert ollama_mount.mode == "rw"
        assert opencode_home == (
            tmp_path / "work" / "auth" / "ws_test" / "opencode" / ".config" / "opencode"
        )
        assert ollama_home == tmp_path / "work" / "auth" / "ws_test" / "ollama" / ".ollama"
        assert (opencode_home / "opencode.json").read_text() == '{"model": "initial"}\n'
        assert (ollama_home / "config.json").read_text() == '{"integrations": {}}\n'
        assert (ollama_home / "id_ed25519").read_text() == "private-key\n"
        assert not (ollama_home / "models").exists()
        assert by_target["/home/agent/.claude"] == base_mount


class TestAgentEnvironmentWithHostAuth:
    @pytest.mark.unit
    def test_passes_known_provider_env_as_compose_placeholders(self) -> None:
        env = run_awf._agent_environment_with_host_auth(
            (("PYTHONUNBUFFERED", "1"),),
            host_env={
                "ANTHROPIC_API_KEY": "secret-anthropic",
                "GEMINI_API_KEY": "secret-gemini",
                "OLLAMA_API_KEY": "secret-ollama",
                "AWF_GITHUB_TOKEN": "ghp_raw_secret",
            },
        )

        assert ("PYTHONUNBUFFERED", "1") in env
        assert ("ANTHROPIC_API_KEY", "${ANTHROPIC_API_KEY}") in env
        assert ("GEMINI_API_KEY", "${GEMINI_API_KEY}") in env
        assert ("OLLAMA_API_KEY", "${OLLAMA_API_KEY}") in env
        assert ("GH_TOKEN", "${AWF_GITHUB_TOKEN}") in env
        assert ("GITHUB_TOKEN", "${AWF_GITHUB_TOKEN}") in env
        assert ("ANTHROPIC_API_KEY", "secret-anthropic") not in env
        assert ("OLLAMA_API_KEY", "secret-ollama") not in env
        assert ("GH_TOKEN", "ghp_raw_secret") not in env
        assert ("GITHUB_TOKEN", "ghp_raw_secret") not in env

    @pytest.mark.unit
    def test_accepts_standard_gh_token_as_compose_placeholder(self) -> None:
        env = run_awf._agent_environment_with_host_auth(
            (),
            host_env={
                "GH_TOKEN": "ghp_raw_secret",
            },
        )

        assert ("GH_TOKEN", "${GH_TOKEN}") in env
        assert ("GITHUB_TOKEN", "${GH_TOKEN}") in env
        assert ("GH_TOKEN", "ghp_raw_secret") not in env
        assert ("GITHUB_TOKEN", "ghp_raw_secret") not in env

    @pytest.mark.unit
    def test_profile_env_wins_over_host_passthrough(self) -> None:
        env = run_awf._agent_environment_with_host_auth(
            (("GEMINI_API_KEY", "profile-value"),),
            host_env={"GEMINI_API_KEY": "host-secret"},
        )

        assert env == (("GEMINI_API_KEY", "profile-value"),)
