"""Root control-plane GC entrypoint (``run_service_workspace_gc``) tests.

The entrypoint wires a volume-removing compose teardown into terminal GC so the
per-workspace Docker volumes that previously leaked are reaped, and runs the
deletion in-container (as root) instead of the host CLI process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.auth_mounts import _shared_claude_base_dir
from awf.node.compose_manager import ComposeTeardownResult
from awf.service import gc_claude_base as gc_claude_base_mod
from awf.service.gc import WorkspaceGCWorktreeRemoveResult, run_service_workspace_gc

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


def _make_superseded_base(work_dir: Path, signature: str) -> Path:
    """Create a completed shared ``~/.claude`` base under ``work_dir`` for GC-B tests."""

    base = _shared_claude_base_dir(work_dir.resolve(), signature)
    base.mkdir(parents=True)
    (base / "blob").write_text("x" * 512)
    return base


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _mock_default_worktree_remover() -> AsyncIterator[None]:
    with patch(
        "awf.service.gc._default_worktree_remover",
        new=AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            )
        ),
    ):
        yield


class _RecordingComposeManager:
    """A fake compose manager capturing teardown calls."""

    def __init__(self, result: ComposeTeardownResult | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result or ComposeTeardownResult(
            status="succeeded", reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED"
        )

    async def teardown_project(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        remove_volumes: bool = True,
    ) -> ComposeTeardownResult:
        self.calls.append(
            {
                "project_name": project_name,
                "compose_file": compose_file,
                "workspace_id": workspace_id,
                "remove_volumes": remove_volumes,
            }
        )
        return self._result


async def _completed_merged_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    updated_at: datetime,
    compose_project_name: str | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title="gc candidate",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.completed.value
        workspace.updated_at = updated_at
        workspace.pr_url = "https://github.com/example/repo/pull/7"
        workspace.pr_number = 7
        workspace.pr_merge_sha = "a" * 40
        workspace.compose_project_name = compose_project_name
        await session.commit()
        return workspace.id


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.unit
async def test_service_gc_wires_volume_removing_teardown_and_deletes_dirs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
        compose_project_name="awf_" + "ws_x",
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    auth = work_dir / "auth" / workspace_id
    compose = work_dir / "compose" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(auth / "codex" / "auth.json", "auth")
    _write(compose / "compose.yml", "services: {}\n")

    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.status == "succeeded"
    # The teardown ran once for the candidate, removing volumes.
    assert len(manager.calls) == 1
    assert manager.calls[0]["remove_volumes"] is True
    assert manager.calls[0]["project_name"] == "awf_ws_x"
    assert manager.calls[0]["workspace_id"] == workspace_id
    assert manager.calls[0]["compose_file"] == compose / "compose.yml"
    # Pressure dirs reclaimed; the durable DB row survives.
    assert not worktree.exists()
    assert not auth.exists()
    assert not compose.exists()
    async with session_factory() as session:
        assert await WorkspaceRepository(session).get(workspace_id) is not None


@pytest.mark.unit
async def test_service_gc_idempotent_teardown_is_not_partial(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A skipped (already-down) teardown is ``ok`` -- the run still succeeds.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    manager = _RecordingComposeManager(
        ComposeTeardownResult(status="skipped", reason_code="NO_COMPOSE_STACK")
    )

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.status == "succeeded"
    assert manager.calls[0]["project_name"] == f"awf_{workspace_id}"
    assert not worktree.exists()


@pytest.mark.unit
async def test_service_gc_teardown_failure_surfaces_as_partial(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    manager = _RecordingComposeManager(
        ComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error="daemon unreachable",
        )
    )

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.status == "partial"
    assert [error.reason_code for error in result.delete_errors] == ["DOCKER_COMPOSE_DOWN_FAILED"]
    # Teardown failed, so deletion was skipped and the worktree is preserved.
    assert worktree.exists()


@pytest.mark.unit
async def test_service_gc_dry_run_does_not_tear_down(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=False,
        min_age_hours=24,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.dry_run is True
    assert manager.calls == []
    assert worktree.exists()


@pytest.mark.unit
async def test_service_gc_runs_companion_prune_when_cache_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
    )
    manager = _RecordingComposeManager()

    prune = AsyncMock(
        return_value={"status": "succeeded", "reason_code": "COMPANION_IMAGE_PRUNE_SUCCEEDED"}
    )
    with patch("awf.node.companion_images.run_companion_image_prune", prune):
        result = await run_service_workspace_gc(
            session_factory,
            work_dir=work_dir,
            template_path=_TEMPLATE,
            execute=True,
            min_age_hours=24,
            companion_image_cache_enabled=True,
            companion_image_retention_hours=72,
            compose_manager=manager,  # type: ignore[arg-type]
            now=now,
        )

    prune.assert_awaited_once_with(72)
    assert result.companion_image_prune == {
        "status": "succeeded",
        "reason_code": "COMPANION_IMAGE_PRUNE_SUCCEEDED",
    }


@pytest.mark.unit
async def test_service_gc_reaps_superseded_claude_bases_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # GC-B wiring (#389): with ``reap_claude_bases`` enabled the entrypoint reaps
    # superseded shared bases and threads the report into the result payload.
    work_dir = tmp_path / "service"
    host_home = tmp_path / "host-home"  # no ~/.claude → no current-signature base
    host_home.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    superseded = _make_superseded_base(work_dir, "sigsuperseded000")
    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        host_home=host_home,
        reap_claude_bases=True,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.claude_base_reap is not None
    assert result.claude_base_reap["reaped"] == ["sigsuperseded000"]
    assert result.to_dict()["claude_base_reap"]["reaped"] == ["sigsuperseded000"]
    assert not superseded.parent.exists()
    assert result.status == "succeeded"


@pytest.mark.unit
async def test_service_gc_reaps_base_pinned_only_by_deleted_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Regression (PRRT_kwDOSJAM6s6HIHN6): when the candidate being deleted holds the
    # last ``base.signature`` pin on a superseded base, the base must be reclaimed in
    # the SAME execute pass. The reaper has to run *after* the candidate auth dir (its
    # pin) is removed; if it runs first the pin still protects the base and the
    # ~1.7GB base leaks until a second GC run.
    work_dir = tmp_path / "service"
    host_home = tmp_path / "host-home"  # no ~/.claude → no current-signature base
    host_home.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    signature = "sigsuperseded000"
    superseded = _make_superseded_base(work_dir, signature)
    workspace_id = await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
    )
    # The candidate being GC'd is the sole pin on the superseded base.
    auth = work_dir / "auth" / workspace_id
    _write(auth / "claude" / "base.signature", signature)
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        host_home=host_home,
        reap_claude_bases=True,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    # The candidate auth dir (and its pin) is gone, so the base is no longer protected
    # and is reaped in this pass — not left behind for a second GC run.
    assert not auth.exists()
    assert result.claude_base_reap is not None
    assert result.claude_base_reap["reaped"] == [signature]
    assert not superseded.parent.exists()
    assert result.status == "succeeded"


@pytest.mark.unit
async def test_service_gc_omits_base_reap_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    superseded = _make_superseded_base(work_dir, "sigsuperseded000")
    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        host_home=host_home,
        reap_claude_bases=False,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.claude_base_reap is None
    assert "claude_base_reap" not in result.to_dict()
    # Disabled: the superseded base is left untouched.
    assert superseded.is_dir()


@pytest.mark.unit
async def test_service_gc_skips_base_reap_with_warning_when_host_home_missing(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # ``reap_claude_bases=True`` with ``host_home=None`` has no host signature to
    # protect the current base with, so GC-B is skipped rather than run blind. The
    # production route always passes ``settings.host_home``; a programmatic caller that
    # forgets it gets a logged warning instead of a silent no-op.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    superseded = _make_superseded_base(work_dir, "sigsuperseded000")
    manager = _RecordingComposeManager()

    with capture_logs() as logs:
        result = await run_service_workspace_gc(
            session_factory,
            work_dir=work_dir,
            template_path=_TEMPLATE,
            execute=True,
            min_age_hours=24,
            host_home=None,
            reap_claude_bases=True,
            compose_manager=manager,  # type: ignore[arg-type]
            now=now,
        )

    assert result.claude_base_reap is None
    assert "claude_base_reap" not in result.to_dict()
    # Skipped, not reaped: the superseded base survives.
    assert superseded.is_dir()
    assert any(
        entry.get("event") == "service_gc_claude_base_reap_skipped_no_host_home"
        and entry.get("log_level") == "warning"
        for entry in logs
    )


@pytest.mark.unit
async def test_service_gc_dry_run_plans_base_reap(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The reaper honors the GC ``execute`` flag, so a dry-run GC previews which
    # superseded bases would be reclaimed without deleting them.
    work_dir = tmp_path / "service"
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    superseded = _make_superseded_base(work_dir, "sigsuperseded000")
    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=False,
        min_age_hours=24,
        host_home=host_home,
        reap_claude_bases=True,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.dry_run is True
    assert result.claude_base_reap is not None
    assert result.claude_base_reap["planned"] == ["sigsuperseded000"]
    assert superseded.is_dir()


@pytest.mark.unit
async def test_service_gc_dry_run_plans_reap_of_base_pinned_only_by_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Regression (PRRT_kwDOSJAM6s6HIepf): a dry run must preview the SAME reaps the
    # matching execute pass would perform. When a superseded base is pinned only by a
    # terminal candidate's ``base.signature``, execute deletes that pin first and reaps
    # the base (see ``test_service_gc_reaps_base_pinned_only_by_deleted_candidate``).
    # The dry run leaves the pin on disk, so without treating the candidate auth dirs as
    # already pruned the preview would mislabel the base ``protected`` and underreport
    # what execute frees.
    work_dir = tmp_path / "service"
    host_home = tmp_path / "host-home"  # no ~/.claude → no current-signature base
    host_home.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    signature = "sigsuperseded000"
    superseded = _make_superseded_base(work_dir, signature)
    workspace_id = await _completed_merged_workspace(
        session_factory,
        updated_at=now - timedelta(hours=200),
    )
    # The candidate slated for GC is the sole pin on the superseded base.
    auth = work_dir / "auth" / workspace_id
    _write(auth / "claude" / "base.signature", signature)
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    manager = _RecordingComposeManager()

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=False,
        min_age_hours=24,
        host_home=host_home,
        reap_claude_bases=True,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.dry_run is True
    assert result.claude_base_reap is not None
    # The base is planned for reap (matching execute), not held back as protected.
    assert result.claude_base_reap["planned"] == [signature]
    assert result.claude_base_reap["protected"] == []
    # Nothing is actually deleted on a dry run.
    assert superseded.is_dir()
    assert auth.exists()


@pytest.mark.unit
async def test_service_gc_base_reap_permission_failure_is_partial(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A permission-denied base reap (leaked disk it could not reclaim) drives the
    # whole run ``partial`` even when no per-workspace candidate failed.
    work_dir = tmp_path / "service"
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    _make_superseded_base(work_dir, "sigsuperseded000")
    manager = _RecordingComposeManager()

    def _denied(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(gc_claude_base_mod.shutil, "rmtree", _denied)

    result = await run_service_workspace_gc(
        session_factory,
        work_dir=work_dir,
        template_path=_TEMPLATE,
        execute=True,
        min_age_hours=24,
        host_home=host_home,
        reap_claude_bases=True,
        compose_manager=manager,  # type: ignore[arg-type]
        now=now,
    )

    assert result.status == "partial"
    assert result.claude_base_reap is not None
    assert result.claude_base_reap["status"] == "partial"
