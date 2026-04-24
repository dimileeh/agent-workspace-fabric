"""ValidationRunner: skip ``alembic upgrade head`` when workspace has no ``alembic.ini``.

Regression guard for the incident where AWF blocked a valid aira-web
(Node.js) workspace for 10+ minutes because the executor tried to run
``alembic upgrade head`` against a repo that has no Alembic config —
aira-web is pure Node and doesn't ship Python migrations.

The semantic of ``requires_database=True`` is "the companion stack needs
Postgres running" — not "run Alembic on the workspace repo." For non-Python
workspaces the companion backend applies its own migrations via its own
entrypoint; AWF must skip the workspace-side Alembic step silently.

Gating rule: if ``workspace_worktree / alembic.ini`` does NOT exist,
skip the migration even when ``requires_database=True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.runtime.validation import ValidationRunner

_COMPOSE_PROJECT = "awf_ws_val_skip"
_COMPOSE_FILE = Path("/fake/compose.yml")


@pytest.fixture
def runner(tmp_path: Path) -> tuple[FakeCommandRunner, ValidationRunner]:
    fake = FakeCommandRunner()
    return fake, ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")


def _make_worktree(tmp_path: Path, *, with_alembic_ini: bool) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    if with_alembic_ini:
        (worktree / "alembic.ini").write_text("[alembic]\n")
    return worktree


def _shell_of(call_args: list[str]) -> str:
    """Return the `sh -lc` payload (the last argv entry) for a fake call."""
    return call_args[-1]


class TestSkipAlembicWhenNoIni:
    @pytest.mark.unit
    async def test_migration_skipped_when_no_alembic_ini(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        tmp_path: Path,
    ) -> None:
        """requires_database=True + no ``alembic.ini`` → migration skipped, run continues.

        Asserts:
          (a) No subprocess invocation for ``alembic upgrade head`` (the
              migration shell payload never hits FakeCommandRunner).
          (b) ``validation.migration_skipped_no_alembic_ini`` log fires.
          (c) Validation continues and runs the next test_command.
        """
        fake, val = runner
        worktree = _make_worktree(tmp_path, with_alembic_ini=False)

        fake.queue_result(returncode=0, stdout="deps installed")  # cmd_01
        fake.queue_result(returncode=0, stdout="tests ok")  # cmd_02 (pytest)

        with structlog.testing.capture_logs() as captured:
            result = await val.run(
                workspace_id="ws_node",
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                test_commands=['uv pip install -e ".[dev]"', "pytest -q"],
                requires_database=True,
                workspace_worktree=worktree,
            )

        # (a) No alembic invocation anywhere in the recorded calls.
        for call in fake.calls:
            assert "alembic upgrade head" not in _shell_of(call.args), (
                f"migration was invoked but workspace has no alembic.ini: {call.args!r}"
            )
        assert result.migration is None

        # (b) Skip log line fired with the expected structured fields.
        skip_events = [
            e for e in captured if e.get("event") == "validation.migration_skipped_no_alembic_ini"
        ]
        assert len(skip_events) == 1, f"expected one skip log; got {skip_events}"
        assert skip_events[0]["workspace_id"] == "ws_node"

        # (c) Validation ran BOTH test commands; both passed.
        assert result.all_passed
        assert len(result.commands) == 2
        assert result.commands[0].command == 'uv pip install -e ".[dev]"'
        assert result.commands[1].command == "pytest -q"
        # Exactly two subprocess calls: cmd_01 and cmd_02. No migration.
        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_migration_runs_when_alembic_ini_present(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        tmp_path: Path,
    ) -> None:
        """requires_database=True + alembic.ini present → migration runs as before."""
        fake, val = runner
        worktree = _make_worktree(tmp_path, with_alembic_ini=True)

        fake.queue_result(returncode=0, stdout="deps installed")  # cmd_01
        fake.queue_result(returncode=0, stdout="migrated")  # migration
        fake.queue_result(returncode=0, stdout="tests ok")  # cmd_02

        result = await val.run(
            workspace_id="ws_py",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=['uv pip install -e ".[dev]"', "pytest -q"],
            requires_database=True,
            workspace_worktree=worktree,
        )

        assert result.all_passed
        assert result.migration is not None
        assert result.migration.ok
        # Three calls: cmd_01, migration, cmd_02.
        assert len(fake.calls) == 3
        # Migration invocation contains the alembic shell.
        migration_shell = _shell_of(fake.calls[1].args)
        assert "alembic upgrade head" in migration_shell

    @pytest.mark.unit
    async def test_migration_skipped_when_requires_database_false(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        tmp_path: Path,
    ) -> None:
        """requires_database=False + alembic.ini present → still skipped.

        Preserves existing behavior: requires_database is the gate for
        opting into the migration step at all.
        """
        fake, val = runner
        worktree = _make_worktree(tmp_path, with_alembic_ini=True)
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_nomig_alembic",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["pytest -q"],
            requires_database=False,
            workspace_worktree=worktree,
        )

        assert result.migration is None
        assert len(fake.calls) == 1
        # No alembic was invoked.
        assert "alembic upgrade head" not in _shell_of(fake.calls[0].args)

    @pytest.mark.unit
    async def test_migration_skipped_when_requires_database_false_no_alembic(
        self,
        runner: tuple[FakeCommandRunner, ValidationRunner],
        tmp_path: Path,
    ) -> None:
        """requires_database=False + no alembic.ini → skipped (existing behavior)."""
        fake, val = runner
        worktree = _make_worktree(tmp_path, with_alembic_ini=False)
        fake.queue_result(returncode=0, stdout="tests ok")

        result = await val.run(
            workspace_id="ws_node_nomig",
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            test_commands=["pytest -q"],
            requires_database=False,
            workspace_worktree=worktree,
        )

        assert result.migration is None
        assert len(fake.calls) == 1
        assert "alembic upgrade head" not in _shell_of(fake.calls[0].args)
