"""Small coverage polish for ``awf.common.*`` — the three modules at
88-96% that don't have a natural test file:

 - ``common/commands.py`` (AsyncioSubprocessRunner.run — uncovered
   because most tests use FakeCommandRunner).
 - ``common/ids.py`` (new_operation_id and new_event_id — tiny helpers
   imported only by repositories that don't exercise both in unit tests).
 - ``common/config.py`` (the cached Settings constructor).
"""

from __future__ import annotations

import weakref

import pytest
from pydantic import ValidationError

import awf.common.config as common_config
from awf.common.callback_events import (
    callback_subscription_matches_event_type,
    is_valid_callback_subscription_event_type,
)
from awf.common.commands import AsyncioSubprocessRunner
from awf.common.config import Settings, get_settings
from awf.common.ids import new_event_id, new_operation_id, new_workspace_id
from awf.common.redaction import redact_secrets


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
    def test_redact_secrets_preserves_empty_text(self) -> None:
        assert redact_secrets("") == ""

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
        monkeypatch.setenv(
            "AWF_DATABASE_URL",
            "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        )
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # lru_cache returns the same instance
        get_settings.cache_clear()

    @pytest.mark.unit
    def test_settings_constructor_uses_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Directly construct Settings without an env file so callers can
        override per-test."""
        monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
        s = Settings(_env_file=None)
        assert s.database_url.startswith("postgresql+asyncpg://")
        assert s.service_name == "awf"
        assert s.env == "local"

    @pytest.mark.unit
    def test_empty_network_posture_cutoff_is_unset(self) -> None:
        settings = Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
            network_posture_open_legacy_cutoff="",
        )

        assert settings.network_posture_open_legacy_cutoff is None

    @pytest.mark.unit
    def test_empty_local_capacity_values_are_unset(self) -> None:
        settings = Settings(
            _env_file=None,
            local_capacity_cpu_cores="",
            local_capacity_memory_gb="",
            local_capacity_dind_slots="",
        )

        assert settings.local_capacity_cpu_cores is None
        assert settings.local_capacity_memory_gb is None
        assert settings.local_capacity_dind_slots is None

    @pytest.mark.unit
    def test_callback_allowed_hosts_accepts_comma_separated_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "AWF_CALLBACKS_ALLOWED_HOSTS",
            "operator.example.com,backup.example.com",
        )

        settings = Settings(_env_file=None)

        assert settings.callbacks_allowed_hosts == (
            "operator.example.com",
            "backup.example.com",
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (
                " Operator.EXAMPLE.com.:8443,backup.example.com:443 ",
                ("operator.example.com", "backup.example.com"),
            ),
            (
                ["Operator.EXAMPLE.com.:8443", "[2606:4700:4700::1111]:443"],
                ("operator.example.com", "2606:4700:4700::1111"),
            ),
        ],
    )
    def test_callback_allowed_hosts_strips_port_suffixes(
        self,
        value: str | list[str],
        expected: tuple[str, ...],
    ) -> None:
        settings = Settings(
            _env_file=None,
            callbacks_allowed_hosts=value,
        )

        assert settings.callbacks_allowed_hosts == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("value", [None, ""])
    def test_callback_allowed_hosts_treats_empty_values_as_unset(
        self,
        value: str | None,
    ) -> None:
        settings = Settings(_env_file=None, callbacks_allowed_hosts=value)

        assert settings.callbacks_allowed_hosts == ()

    @pytest.mark.unit
    def test_invalid_callback_allowed_hosts_raises_validation_error(self) -> None:
        with pytest.raises(
            ValidationError,
            match="callbacks_allowed_hosts must be a comma-separated string, list, or tuple",
        ):
            Settings(_env_file=None, callbacks_allowed_hosts=123)

    @pytest.mark.unit
    def test_service_startup_log_tail_lines_defaults_to_200(self) -> None:
        settings = Settings(_env_file=None)

        assert settings.worker_service_startup_log_tail_lines == 200

    @pytest.mark.unit
    def test_service_startup_log_tail_lines_reads_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_WORKER_SERVICE_STARTUP_LOG_TAIL_LINES", "42")

        settings = Settings(_env_file=None)

        assert settings.worker_service_startup_log_tail_lines == 42

    @pytest.mark.unit
    def test_service_startup_log_tail_lines_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, worker_service_startup_log_tail_lines=0)

    @pytest.mark.unit
    def test_settings_identity_ref_uses_object_identity(self) -> None:
        settings = Settings(_env_file=None)
        other_settings = Settings(_env_file=None)
        reference = common_config._SettingsIdentityRef(settings)  # noqa: SLF001

        assert reference.__eq__(object()) is False
        assert reference == reference
        assert reference.__eq__(common_config._SettingsIdentityRef(other_settings)) is False  # noqa: SLF001

    @pytest.mark.unit
    def test_discard_settings_constructor_fields_ignores_plain_weakrefs(self) -> None:
        settings = Settings(_env_file=None)

        common_config._discard_settings_constructor_fields(weakref.ref(settings))  # noqa: SLF001

        assert common_config.settings_constructor_fields(settings) == frozenset()


class TestRedaction:
    @pytest.mark.unit
    def test_empty_text_is_returned_unchanged(self) -> None:
        assert redact_secrets("") == ""


class TestCallbackEventPolicy:
    @pytest.mark.unit
    def test_subscription_event_type_validation_and_matching(self) -> None:
        assert is_valid_callback_subscription_event_type("workspace.*")
        assert is_valid_callback_subscription_event_type("workspace.created")
        assert not is_valid_callback_subscription_event_type("internal.secret")

        assert callback_subscription_matches_event_type(
            "workspace.*",
            "workspace.created",
        )
        assert callback_subscription_matches_event_type(
            "workspace.*",
            "workspace.state_changed",
        )
        assert callback_subscription_matches_event_type(
            "workspace.created",
            "workspace.created",
        )
        assert not callback_subscription_matches_event_type(
            "workspace.*",
            "workspace.internal_secret",
        )
        assert not callback_subscription_matches_event_type(
            "merge.*",
            "workspace.created",
        )
        assert not callback_subscription_matches_event_type(
            "workspace.created",
            "operation.state_changed",
        )
        assert not callback_subscription_matches_event_type(
            "workspace.*",
            "internal.secret",
        )
