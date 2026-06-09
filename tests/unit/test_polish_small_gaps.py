"""Polish coverage — sub-100% modules with 1-5 line gaps.

Each test here targets a specific uncovered line or branch. Batched
in one file to avoid scattering tiny test modules.

Covers:

 - ``runtime/validation.ValidationResult.first_failure`` — the
   migration-failed branch.
 - ``runtime/validation.ValidationRunner._format_display`` — the
   ``sh -c`` preamble-stripping path.
 - ``node/provisioner._load_and_claim`` — skip_unknown log path.
 - ``node/provisioner._mark_failed`` — from_status mismatch path.
 - ``node/git_manager.GitManager.work_dir`` property.
 - ``node/git_manager._slugify_repo`` — tail=.git suffix stripping +
   empty-tail fallback.
 - ``control/validation_fix_cycle.read_output_tail`` — OSError during stat.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.validation_fix_cycle import read_output_tail
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitManager, _slugify_repo
from awf.node.provisioner import Provisioner
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationResult,
    ValidationRunner,
)
from tests.postgres import postgres_test_engine

# ── validation ─────────────────────────────────────────────────────────────


class TestValidationResult:
    @pytest.mark.unit
    def test_first_failure_returns_migration_on_migration_fail(self) -> None:
        """Line 84: the migration-failed branch of first_failure."""
        migration = ValidationCommandResult(
            command="alembic upgrade head",
            returncode=1,
            duration_seconds=0.1,
            stdout_path=Path("/tmp/m.stdout"),
            stderr_path=Path("/tmp/m.stderr"),
        )
        report = ValidationResult(migration=migration, commands=())
        assert report.all_passed is False
        assert report.first_failure is migration


class TestValidationDisplay:
    """ValidationRunner formats its output's ``command`` field
    differently depending on whether the invocation was via our
    internal ``sh -c`` wrapper (so the preamble needs stripping) or a
    direct argv list."""

    @pytest.mark.unit
    async def test_sh_preamble_stripped_when_starts_with_venv_activate(
        self, tmp_path: Path
    ) -> None:
        """Drives the REAL ``ValidationRunner._exec`` path with a fake
        command runner and asserts the production formatter — not a
        reimplementation — strips the preamble. A regression in the
        actual code (e.g. changing the prefix test) would fail here
        instead of being silently shadowed by the test's own logic."""
        from awf.common.commands import FakeCommandRunner
        from awf.runtime import validation as validation_mod

        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="ok\n")
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path)
        preamble = validation_mod._VENV_ACTIVATE_PREAMBLE
        result = await runner._exec(
            compose_project="awf_x",
            compose_file=Path("/tmp/c.yml"),
            cli_args=["sh", "-c", f"{preamble}pytest -q"],
            label="unit",
            artifacts_dir=tmp_path,
        )
        assert result.command == "pytest -q"

    @pytest.mark.unit
    async def test_non_sh_args_are_quoted_via_shlex(self, tmp_path: Path) -> None:
        """Covers validation.py line 205 — the non-sh display branch
        runs through ``shlex.quote`` on each arg. Driven via the real
        ``_exec`` so the production path is actually exercised."""
        from awf.common.commands import FakeCommandRunner

        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="")
        runner = ValidationRunner(runner=fake, artifacts_dir=tmp_path)
        result = await runner._exec(
            compose_project="awf_x",
            compose_file=Path("/tmp/c.yml"),
            cli_args=["pytest", "-q", "tests/with spaces/"],
            label="unit",
            artifacts_dir=tmp_path,
        )
        assert "pytest -q" in result.command
        assert "'tests/with spaces/'" in result.command or '"tests/with spaces/"' in result.command


# ── provisioner ────────────────────────────────────────────────────────────


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestProvisionerSkipUnknown:
    @pytest.mark.unit
    async def test_load_and_claim_skips_unknown_workspace(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Covers lines 134-135: workspace not in DB → log + return None."""

        class _Stub:
            async def add_worktree(self, **_kw: Any) -> Any:
                raise AssertionError("shouldn't be called")

        from awf.node.provisioner import ProvisionerConfig

        prov = Provisioner(
            session_factory=factory,
            git=_Stub(),  # type: ignore[arg-type]
            config=ProvisionerConfig(node_id="test-node"),
        )
        async with factory() as s:
            ws = await prov._load_and_claim(s, "ws_nonexistent")
            assert ws is None

    @pytest.mark.unit
    async def test_mark_failed_noops_when_status_diverged(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Covers line 170: if the workspace moved to a different state
        between the error and the mark_failed attempt, we respect that
        — don't force ``failed``."""

        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url="r",
                branch_base="b",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                requires_database=False,
            )
            await s.commit()
            ws_id = ws.id

        from awf.node.provisioner import ProvisionerConfig

        prov = Provisioner(
            session_factory=factory,
            git=object(),  # type: ignore[arg-type]
            config=ProvisionerConfig(node_id="test-node"),
        )
        # ws.status is 'requested' (the initial state). Call _mark_failed
        # claiming from_status=provisioning — a mismatch — so it returns
        # without transitioning.
        await prov._mark_failed(
            workspace_id=ws_id,
            failure_reason=FailureReason.infrastructure_failure,
            message="we'd like to mark it failed, but the state diverged",
            from_status=WorkspaceStatus.provisioning,
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # Status preserved, not overridden.
            assert ws.status == "requested"

    @pytest.mark.unit
    async def test_mark_failed_pre_launch_leaves_compose_project_name_null(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Pre-launch failures (compose_launched=False) must not set
        compose_project_name.  A workspace that failed before Docker
        Compose was launched never bound a host port, so it must not
        block port admission in find_host_port_conflicts."""

        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url="r",
                branch_base="b",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                requires_database=False,
            )
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
            await s.commit()
            ws_id = ws.id

        from awf.node.provisioner import ProvisionerConfig

        prov = Provisioner(
            session_factory=factory,
            git=object(),  # type: ignore[arg-type]
            config=ProvisionerConfig(node_id="test-node"),
        )
        await prov._mark_failed(
            workspace_id=ws_id,
            failure_reason=FailureReason.infrastructure_failure,
            message="port conflict before compose launch",
            from_status=WorkspaceStatus.provisioning,
            compose_launched=False,
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.compose_project_name is None

    @pytest.mark.unit
    async def test_mark_failed_post_compose_sets_compose_project_name(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Post-compose failures (compose_launched=True) must set
        compose_project_name so the cleanup worker and port-conflict
        check can find the Docker project."""

        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url="r",
                branch_base="b",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                requires_database=False,
            )
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
            await s.commit()
            ws_id = ws.id

        from awf.node.provisioner import ProvisionerConfig

        prov = Provisioner(
            session_factory=factory,
            git=object(),  # type: ignore[arg-type]
            config=ProvisionerConfig(node_id="test-node"),
        )
        await prov._mark_failed(
            workspace_id=ws_id,
            failure_reason=FailureReason.service_startup_failure,
            message="compose stack failed to start",
            from_status=WorkspaceStatus.provisioning,
            compose_launched=True,
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.compose_project_name == f"awf_{ws_id}"


# ── git_manager ────────────────────────────────────────────────────────────


class TestGitManagerSmallHelpers:
    @pytest.mark.unit
    def test_work_dir_property_exposes_init_arg(self, tmp_path: Path) -> None:
        """Line 91: ``work_dir`` property — trivial, but verifies the
        attribute is reachable for operators inspecting a GitManager."""
        gm = GitManager(tmp_path / "awf-git")
        assert gm.work_dir == tmp_path / "awf-git"


class TestMirrorSlug:
    @pytest.mark.unit
    def test_strips_dot_git_suffix(self) -> None:
        """Line 375 covers the ``tail.endswith('.git')`` branch."""
        assert _slugify_repo("git@github.com:dimileeh/aira-web.git").startswith("aira-web")

    @pytest.mark.unit
    def test_empty_tail_falls_back_to_repo(self) -> None:
        """Edge case: when the URL's tail reduces to an empty string
        (after ``.git`` strip + regex sanitize), the slugifier falls
        back to the literal ``repo`` sentinel.

        We construct a URL whose last path segment is literally ``.git``
        — after stripping the ``.git`` suffix, the tail is empty, and
        the sub-then-truthy-fallback path fires."""
        slug = _slugify_repo("https://example.com/.git")
        assert slug == "repo"


# ── validation_fix_cycle ───────────────────────────────────────────────────


class TestReadTailOSError:
    @pytest.mark.unit
    def test_oserror_on_stat_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Lines 91-92: stat() failure (permission denied, disk full,
        etc.) is treated as "no tail available" — the validation-fix
        loop must not crash on an artifact stat error.

        ``path.exists()`` on the happy path calls ``stat`` internally
        and catches OSError, so a blanket monkeypatch would short-circuit
        the function at line 87 before reaching line 90. Use a counter
        that lets exists() see the first stat call succeed then raises
        on the EXPLICIT stat() invocation at line 90."""
        p = tmp_path / "artifact.stdout"
        p.write_text("content here")
        real_stat = Path.stat
        state = {"first_done": False}

        def _boom(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == p and state["first_done"]:
                raise OSError("simulated disk failure")
            state["first_done"] = True
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom)
        assert read_output_tail(p, max_chars=100) == ""
