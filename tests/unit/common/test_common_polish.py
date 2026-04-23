"""Small coverage polish for ``awf.common.*`` — the three modules at
88-96% that don't have a natural test file:

 - ``common/commands.py`` (AsyncioSubprocessRunner.run — uncovered
   because most tests use FakeCommandRunner).
 - ``common/ids.py`` (new_operation_id and new_event_id — tiny helpers
   imported only by repositories that don't exercise both in unit tests).
 - ``common/config.py`` (the cached Settings constructor).
"""

from __future__ import annotations

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from awf.common.config import Settings, get_settings
from awf.common.ids import new_event_id, new_operation_id, new_workspace_id


class TestAsyncioSubprocessRunner:
    @pytest.mark.unit
    async def test_runs_real_subprocess_captures_stdout(self) -> None:
        """Spawn a trivial subprocess and verify stdout/returncode are
        captured. We use /bin/echo which is ubiquitous on Linux + macOS."""
        runner = AsyncioSubprocessRunner()
        result = await runner.run(["/bin/echo", "hello"])
        assert result.returncode == 0
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.ok is True

    @pytest.mark.unit
    async def test_runs_real_subprocess_captures_nonzero_exit(self) -> None:
        runner = AsyncioSubprocessRunner()
        # /bin/sh -c 'exit 7' — portable way to produce a non-zero code.
        result = await runner.run(["/bin/sh", "-c", "exit 7"])
        assert result.returncode == 7
        assert result.ok is False

    @pytest.mark.unit
    async def test_runs_real_subprocess_with_stdin_piped(self) -> None:
        runner = AsyncioSubprocessRunner()
        result = await runner.run(["/bin/cat"], input_bytes=b"ping\n")
        assert result.returncode == 0
        assert result.stdout == "ping\n"

    @pytest.mark.unit
    async def test_runs_real_subprocess_with_cwd(self, tmp_path) -> None:
        runner = AsyncioSubprocessRunner()
        result = await runner.run(["/bin/pwd"], cwd=str(tmp_path))
        assert result.returncode == 0
        assert tmp_path.name in result.stdout


class TestIds:
    @pytest.mark.unit
    def test_workspace_id_prefix(self) -> None:
        assert new_workspace_id().startswith("ws_")

    @pytest.mark.unit
    def test_operation_id_prefix(self) -> None:
        assert new_operation_id().startswith("op_")

    @pytest.mark.unit
    def test_event_id_prefix(self) -> None:
        assert new_event_id().startswith("evt_")

    @pytest.mark.unit
    def test_ids_are_unique_per_call(self) -> None:
        assert new_workspace_id() != new_workspace_id()
        assert new_operation_id() != new_operation_id()
        assert new_event_id() != new_event_id()


class TestSettings:
    @pytest.mark.unit
    def test_get_settings_returns_cached_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear cache so the test drives a fresh Settings() call.
        get_settings.cache_clear()
        # Explicit env to satisfy any required field in future extensions.
        monkeypatch.setenv("AWF_DATABASE_URL", "sqlite+aiosqlite:///./x.db")
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # lru_cache returns the same instance
        get_settings.cache_clear()

    @pytest.mark.unit
    def test_settings_constructor_uses_defaults(self) -> None:
        """Directly construct Settings without an env file so callers can
        override per-test."""
        s = Settings(_env_file=None)
        assert s.database_url.startswith("sqlite")
        assert s.service_name == "awf"
        assert s.env == "local"
