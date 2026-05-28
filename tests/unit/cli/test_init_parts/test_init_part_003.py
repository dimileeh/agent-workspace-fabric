"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.unit.cli.test_init_parts._bootstrap_helper import invoke_init_service_bootstrap


def _docker_diagnostic(status: str = "ok") -> Any:
    from awf.service.doctor.models import DoctorDiagnostic

    return DoctorDiagnostic(
        id="docker",
        label="Docker",
        status=status,
        reason="DOCKER_OK" if status == "ok" else "DOCKER_DAEMON_UNREACHABLE",
        message=(
            "Docker daemon is reachable."
            if status == "ok"
            else "Docker is installed but the daemon is not reachable."
        ),
        action=(
            "No action required."
            if status == "ok"
            else "Start Docker Desktop or verify AWF_DOCKER_HOST."
        ),
        source="checks.docker",
    )


def _doctor_report(*diagnostics: Any) -> Any:
    from awf.service.doctor.models import DoctorReport

    overall = "ok" if all(getattr(d, "status", "ok") == "ok" for d in diagnostics) else "fail"
    return DoctorReport(
        service="awf",
        status=overall,
        diagnostics=tuple(diagnostics),
    )


def _stub_bootstrap_mode(
    monkeypatch: pytest.MonkeyPatch,
    *,
    docker_status: str = "ok",
    bootstrap_result: Any = None,
    bootstrap_error: Exception | None = None,
    asset_root: Path | None = None,
) -> dict[str, Any]:
    """Stub doctor + service bootstrap for ``awf init`` (no-path) tests."""

    from awf.common import config as common_config
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    captured: dict[str, Any] = {"bootstrap_calls": [], "settings_instances": []}
    settings = object()

    class StubSettings:
        """Minimal Settings double for helper-backed bootstrap tests."""

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            captured["settings_instances"].append(self)

    monkeypatch.setattr(common_config, "Settings", StubSettings)

    def _resolve_service_settings(*_args: object, **_kwargs: object) -> object:
        return settings

    monkeypatch.setattr(config_mod, "resolve_service_settings", _resolve_service_settings)
    captured["settings"] = settings

    docker_diag = _docker_diagnostic(docker_status)
    report = _doctor_report(docker_diag)

    async def _collect_doctor_report(_settings: object, **kwargs: Any) -> Any:
        captured["doctor_kwargs"] = kwargs
        captured["doctor_settings"] = _settings
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)

    if bootstrap_result is None:
        from awf.service.bootstrap import ServiceBootstrapResult

        bootstrap_result = ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        captured["bootstrap_calls"].append({"settings": received_settings, **kwargs})
        if bootstrap_error is not None:
            raise bootstrap_error
        return bootstrap_result

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)
    return captured


def _fail_path_write(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_path: str,
    message: str = "permission denied",
) -> None:
    """Patch Path.open to fail writes for one expected path."""
    original_open = Path.open
    failing_path_resolved = Path(failing_path).resolve()

    def _open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        """Raise a synthetic write failure only for the configured path."""
        if self.resolve() == failing_path_resolved and {"w", "a", "x", "+"}.intersection(mode):
            raise OSError(message)
        return original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", _open)


def _create_path_before_exclusive_open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_path: str,
    contents: bytes,
) -> None:
    """Create a path just before an exclusive open attempts to seed it."""
    original_open = Path.open
    target_path_resolved = Path(target_path).resolve()

    def _open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        if self.resolve() == target_path_resolved and "x" in mode and not self.exists():
            with original_open(self, "wb") as handle:
                handle.write(contents)
        return original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", _open)


def _fail_path_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_path: str,
    message: str = "permission denied",
) -> None:
    """Patch Path.read_bytes to fail for one expected path."""
    original_read_bytes = Path.read_bytes
    failing_path_resolved = Path(failing_path).resolve()

    def _read_bytes(self: Path) -> bytes:
        """Raise a synthetic read failure only for the configured path."""
        if self.resolve() == failing_path_resolved:
            raise OSError(message)
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)


def _fail_path_mkdir(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_path: str,
    message: str = "permission denied",
) -> None:
    """Patch Path.mkdir to fail for one expected path."""
    original_mkdir = Path.mkdir
    failing_path_resolved = Path(failing_path).resolve()

    def _mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Raise a synthetic mkdir failure only for the configured path."""
        if self.resolve() == failing_path_resolved:
            raise OSError(message)
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _mkdir)


def _stub_local_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_status: str = "ok",
    doctor_status: str = "ok",
    preview_smoke_payload: bool = False,
) -> None:
    async def _collect_service_status(_settings: object, **kwargs: object) -> dict[str, object]:
        return {
            "service": "awf",
            "status": service_status,
            "checks": {},
            "agent_readiness": {"status": service_status},
        }

    async def _collect_doctor_report(_settings: object, **_kwargs: object) -> object:
        return SimpleNamespace(status=doctor_status, diagnostics=())

    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'unknown')}\n",
    )
    if preview_smoke_payload:

        def _preview_project_onboarding(_path: Path, **_kwargs: object) -> object:
            return SimpleNamespace(
                path=_path,
                draft=SimpleNamespace(template="generic"),
                smoke_request={"dummy": "payload"},
                to_dict=lambda: {
                    "path": str(_path),
                    "draft": {"template": "generic"},
                    "diagnostics": {},
                },
            )

        monkeypatch.setattr(
            "awf.profiles.onboarding.preview_project_onboarding",
            _preview_project_onboarding,
        )


def _read_written_profile(project: Path) -> dict[str, Any]:
    raw = yaml.safe_load((project / ".awf" / "workspace.yml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    awf_profile = raw.get("awf")
    assert isinstance(awf_profile, dict)
    return awf_profile


@pytest.mark.unit
def test_seed_env_file_removes_partial_file_after_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not leave a broken env file when a write fails after creation."""
    from awf.cli import init_ops as cli_main

    env_file = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_example.write_bytes(b"AWF_API_TOKEN=local\n")
    original_open = Path.open

    class FailingWriter:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> FailingWriter:
            self._handle.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._handle.__exit__(*args)

        def write(self, contents: bytes) -> int:
            self._handle.write(contents[:8])
            self._handle.flush()
            raise OSError("disk quota exceeded")

    def _open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        handle = original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )
        if self == env_file and mode == "xb":
            return FailingWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", _open)

    action, error, overlay_keys = cli_main._seed_env_file(env_file, env_example)  # noqa: SLF001

    assert action == "write_failed"
    assert error is not None
    assert error["operation"] == "write_env"
    assert error["message"] == "disk quota exceeded"
    assert overlay_keys == ()
    assert not env_file.exists()


@pytest.mark.unit
def test_init_env_warning_uses_display_ready_payload_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not reinterpret already-normalized env error payload paths."""
    from awf.cli import init_ops as cli_main

    monkeypatch.chdir(tmp_path)

    warning = cli_main._init_env_warning(  # noqa: SLF001
        {
            "operation": "write_env",
            "path": "/display/env",
            "env_file": "/display/env",
            "env_example": "/display/env.example",
            "message": "permission denied",
        }
    )

    assert warning == (
        "  warning: could not write /display/env from /display/env.example: permission denied"
    )


@pytest.mark.unit
def test_init_env_warning_describes_overlay_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Describe overlay read failures as reads, not env writes."""
    from awf.cli import init_ops as cli_main

    monkeypatch.chdir(tmp_path)

    warning = cli_main._init_env_warning(  # noqa: SLF001
        {
            "operation": "read_overlay",
            "path": ".env",
            "env_file": "docker/compose/.env",
            "env_example": "docker/compose/.env.example",
            "message": "permission denied",
        }
    )

    assert warning == (
        "  warning: could not read .env while seeding docker/compose/.env "
        "from docker/compose/.env.example: permission denied"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failure_mode", "expected_operation", "expected_path"),
    (
        ("mkdir", "create_parent_directory", "."),
        ("read", "read_example", ".env.example"),
        ("write", "write_env", ".env"),
    ),
)
def test_init_without_path_json_marks_env_write_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
    expected_operation: str,
    expected_path: str,
) -> None:
    """Expose env copy failures in machine-readable init output."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)
    if failure_mode == "mkdir":
        _fail_path_mkdir(monkeypatch, failing_path=".")
    elif failure_mode == "read":
        _fail_path_read_bytes(monkeypatch, failing_path=".env.example")
    else:
        _fail_path_write(monkeypatch, failing_path=".env")

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": expected_operation,
        "path": expected_path,
        "env_file": ".env",
        "env_example": ".env.example",
        "message": "permission denied",
    }


@pytest.mark.unit
def test_init_without_path_json_normalizes_asset_root_env_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep machine-readable env failure paths relative to the launch directory."""
    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_root / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    _stub_bootstrap_mode(monkeypatch, asset_root=workspace_root)
    _fail_path_write(monkeypatch, failing_path="../docker/compose/.env")

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "write_env",
        "path": "../docker/compose/.env",
        "env_file": "../docker/compose/.env",
        "env_example": "../.env.example",
        "message": "permission denied",
    }


@pytest.mark.unit
def test_init_without_path_json_marks_env_overlay_read_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose overlay read failures without confusing the seed source."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AWF_API_TOKEN=root\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)
    _fail_path_read_bytes(monkeypatch, failing_path=".env")

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "read_overlay",
        "path": ".env",
        "env_file": "docker/compose/.env",
        "env_example": "docker/compose/.env.example",
        "message": "permission denied",
    }


@pytest.mark.unit
def test_merge_env_seed_keeps_first_key_comment_after_header_without_separator() -> None:
    """Keep key-specific first-assignment comments adjacent to the merged key."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_POSTGRES_PASSWORD=compose-example",
                    "AWF_API_TOKEN=compose-example",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "# Existing root .env migrated by awf init.",
                    "# Existing API token override",
                    "AWF_API_TOKEN=migrated-token",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "AWF_POSTGRES_PASSWORD=compose-example",
                "# Existing API token override",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_splits_header_at_last_adjacent_key_comment() -> None:
    """Only the final adjacent key comment should move below a header block."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_POSTGRES_PASSWORD=compose-example",
                    "AWF_API_TOKEN=compose-example",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "# Existing API token values migrated by awf init.",
                    "# Operator API token override",
                    "AWF_API_TOKEN=migrated-token",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Existing API token values migrated by awf init.",
                "AWF_POSTGRES_PASSWORD=compose-example",
                "# Operator API token override",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_treats_single_word_key_comment_as_assignment_context() -> None:
    """A single significant key word should still identify assignment context."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "# Compose service defaults.",
                    "AWF_TOKEN=compose-example",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "# Existing root .env migrated by awf init.",
                    "# Provider access token",
                    "AWF_TOKEN=migrated-token",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Compose service defaults.",
                "# Provider access token",
                "AWF_TOKEN=migrated-token",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_keeps_first_key_comment_when_seed_has_header() -> None:
    """Do not discard key-specific first-assignment comments under a seed header."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "# Compose service defaults.",
                    "AWF_POSTGRES_PASSWORD=compose-example",
                    "AWF_API_TOKEN=compose-example",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "# Existing root .env migrated by awf init.",
                    "# Existing API token override",
                    "AWF_API_TOKEN=migrated-token",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Compose service defaults.",
                "AWF_POSTGRES_PASSWORD=compose-example",
                "# Existing API token override",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_keeps_short_first_key_context_when_seed_has_header() -> None:
    """Short first-key comment blocks are more likely key docs than file headers."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "# Compose service defaults.",
                    "AWF_API_TOKEN=compose-example",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "# Created for staging access.",
                    "# Rotate this value with release credentials.",
                    "AWF_API_TOKEN=migrated-token",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Compose service defaults.",
                "# Created for staging access.",
                "# Rotate this value with release credentials.",
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_preserves_overlay_header_when_seed_starts_with_blank_line() -> None:
    """A blank-only seed preamble should not suppress the overlay file header."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"\nAWF_API_TOKEN=compose-example\n",
        (
            "\n".join(
                [
                    "# Existing root .env migrated by awf init.",
                    "# Operators may keep local service overrides here.",
                    "AWF_API_TOKEN=migrated-token",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "# Operators may keep local service overrides here.",
                "",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_matches_overlay_keys_case_insensitively() -> None:
    """Lowercase root keys should override template keys without becoming duplicates."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        b"awf_api_token=migrated-token\n",
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_strips_export_prefix_from_overlay_assignments() -> None:
    """Write shell-compatible root env assignments as Compose env assignments."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "# Existing token override",
                    "export awf_api_token=migrated-token",
                    "# Operator root-only key",
                    "export AWF_ROOT_ONLY=root-value",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ("AWF_ROOT_ONLY",)
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "# Existing token override",
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "# Operator root-only key",
                "AWF_ROOT_ONLY=root-value",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_normalizes_overlay_assignment_without_trailing_newline() -> None:
    """A root overlay EOF assignment must not absorb the following seed line."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"AWF_API_TOKEN=compose-example\nAWF_COMPOSE_ONLY=compose-default\n",
        b"AWF_API_TOKEN=migrated-token",
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "AWF_API_TOKEN=migrated-token\nAWF_COMPOSE_ONLY=compose-default\n"
    )


@pytest.mark.unit
def test_merge_env_seed_normalizes_overlay_only_assignment_without_trailing_newline() -> None:
    """A root-only overlay EOF assignment should keep the merged env newline."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"AWF_API_TOKEN=compose-example\n",
        b"AWF_ROOT_ONLY=root-value",
    )

    assert overlay_only_keys == ("AWF_ROOT_ONLY",)
    assert merged_contents.decode("utf-8") == (
        "AWF_API_TOKEN=compose-example\nAWF_ROOT_ONLY=root-value\n"
    )


@pytest.mark.unit
def test_merge_env_seed_appends_trailing_shared_overlay_context_after_seed_lines() -> None:
    """Overlay EOF comments for shared keys belong at the merged file tail."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "",
                    "# Keep this EOF note at the end.",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "",
                "# Keep this EOF note at the end.",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seed_text", "overlay_text"),
    (
        ('AWF_API_TOKEN="template\ncontinued"\n', "AWF_API_TOKEN=root\n"),
        ("AWF_API_TOKEN=template\n", 'AWF_API_TOKEN="root\ncontinued"\n'),
        ("AWF_API_TOKEN=template\\\ncontinued\n", "AWF_API_TOKEN=root\n"),
        ("AWF_API_TOKEN=template\n", "AWF_API_TOKEN=root\\\ncontinued\n"),
    ),
)
def test_merge_env_seed_contents_rejects_multiline_dotenv_values(
    seed_text: str,
    overlay_text: str,
) -> None:
    """Do not let the line-oriented merge mangle multi-line dotenv values."""
    from awf.cli import init_ops as cli_main

    with pytest.raises(ValueError, match="multi-line dotenv"):
        cli_main._merge_env_seed_contents(  # noqa: SLF001
            seed_text.encode("utf-8"),
            overlay_text.encode("utf-8"),
        )


@pytest.mark.unit
def test_merge_env_seed_contents_allows_escaped_trailing_backslash_values() -> None:
    """Treat paired trailing backslashes as a single-line dotenv value."""
    from awf.cli import init_ops as cli_main

    seed_windows_path = "AWF_WINDOWS_PATH=C:" + "\\" * 2 + "\n"
    overlay_windows_path = "AWF_ROOT_PATH=C:" + "\\" * 2 + "\n"

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (seed_windows_path + "AWF_API_TOKEN=compose\n").encode("utf-8"),
        ("AWF_API_TOKEN=root\n" + overlay_windows_path).encode("utf-8"),
    )

    assert overlay_only_keys == ("AWF_ROOT_PATH",)
    assert merged_contents.decode("utf-8") == (
        seed_windows_path + "AWF_API_TOKEN=root\n" + overlay_windows_path
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seed_contents", "overlay_contents"),
    (
        (b"AWF_API_TOKEN=compose\nINVALID=\xff\n", b"AWF_API_TOKEN=root\n"),
        (b"AWF_API_TOKEN=compose\n", b"AWF_API_TOKEN=root\nINVALID=\xff\n"),
    ),
)
def test_merge_env_seed_contents_rejects_non_utf8_dotenv_contents(
    seed_contents: bytes,
    overlay_contents: bytes,
) -> None:
    """Expose undecodable dotenv inputs instead of silently skipping overlays."""
    from awf.cli import init_ops as cli_main

    with pytest.raises(ValueError, match="UTF-8"):
        cli_main._merge_env_seed_contents(  # noqa: SLF001
            seed_contents,
            overlay_contents,
        )


@pytest.mark.unit
def test_merge_env_seed_contents_preserves_context_between_duplicate_overlay_keys() -> None:
    """Keep comments between duplicate overlay assignments with the final value."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_DOCKER_HOST=",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "AWF_DOCKER_HOST=unix:///tmp/first-docker.sock",
                    "",
                    "# Regenerated duplicate Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/second-docker.sock",
                    "",
                    "# Operator final Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Regenerated duplicate Docker host context",
                "",
                "# Operator final Docker host context",
                "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_contents_preserves_context_before_first_duplicate_overlay_key() -> None:
    """Keep context before the first duplicate with the final overlay value."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_DOCKER_HOST=",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "# Operator Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/first-docker.sock",
                    "# Operator final Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "# Operator Docker host context",
                "# Operator final Docker host context",
                "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_contents_preserves_context_before_duplicate_overlay_only_key() -> None:
    """Keep comments before the first overlay-only duplicate with the final value."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"AWF_API_TOKEN=compose-example\n",
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "# Operator endpoint settings",
                    "# Migrated from the root env file",
                    "AWF_EXTRA_ENDPOINT=https://first.example.test",
                    "",
                    "# Operator final endpoint context",
                    "AWF_EXTRA_ENDPOINT=https://final.example.test",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ("AWF_EXTRA_ENDPOINT",)
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "# Operator endpoint settings",
                "# Migrated from the root env file",
                "",
                "# Operator final endpoint context",
                "AWF_EXTRA_ENDPOINT=https://final.example.test",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_keeps_context_before_first_overlay_only_duplicate_with_blank_separator() -> (
    None
):
    """Keep non-comment notes before the first root-only duplicate with the final value."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"AWF_API_TOKEN=compose-example\n",
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "",
                    "Operator endpoint migrated from the root env file",
                    "AWF_EXTRA_ENDPOINT=https://first.example.test",
                    "# Final endpoint",
                    "AWF_EXTRA_ENDPOINT=https://final.example.test",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ("AWF_EXTRA_ENDPOINT",)
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "Operator endpoint migrated from the root env file",
                "# Final endpoint",
                "AWF_EXTRA_ENDPOINT=https://final.example.test",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_keeps_single_comment_before_duplicate_overlay_only_key() -> None:
    """Keep a single adjacent comment before the first root-only duplicate."""
    from awf.cli import init_ops as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"AWF_API_TOKEN=compose-example\n",
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "# Operator endpoint",
                    "AWF_EXTRA_ENDPOINT=https://first.example.test",
                    "# Final endpoint",
                    "AWF_EXTRA_ENDPOINT=https://final.example.test",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ("AWF_EXTRA_ENDPOINT",)
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "# Operator endpoint",
                "# Final endpoint",
                "AWF_EXTRA_ENDPOINT=https://final.example.test",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_json_marks_multiline_env_overlay_merge_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose unsupported multi-line overlay merges instead of writing corruption."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        'AWF_API_TOKEN="root-token-line-one\nroot-token-line-two"\n',
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "merge_overlay",
        "path": ".env",
        "env_file": "docker/compose/.env",
        "env_example": "docker/compose/.env.example",
        "message": (
            "unsupported multi-line dotenv values; env seeding merge only supports "
            "single-line assignments"
        ),
    }
    assert not (compose / ".env").exists()
    assert "root-token-line-one" not in result.output
    assert "root-token-line-two" not in result.output


@pytest.mark.unit
def test_init_without_path_json_marks_non_utf8_env_overlay_merge_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose invalid UTF-8 overlays instead of writing a template-only env file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env").write_bytes(b"AWF_API_TOKEN=root\nINVALID=\xff\n")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "BOOTSTRAP_LOCAL_CHECKS_FAILED"
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "merge_overlay",
        "path": ".env",
        "env_file": "docker/compose/.env",
        "env_example": "docker/compose/.env.example",
        "message": "env seeding merge requires UTF-8 dotenv files",
    }
    assert not (compose / ".env").exists()
    assert "AWF_API_TOKEN=root" not in result.output


@pytest.mark.unit
def test_init_without_path_runs_docker_availability_check_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    assert "Docker is installed but the daemon is not reachable" in result.output
    assert not (tmp_path / "state").exists()


@pytest.mark.unit
def test_init_without_path_prints_env_success_before_docker_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Show the created env file even when Docker blocks bootstrap."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    env_message = "wrote .env from .env.example"
    docker_failure = "Docker is not available; cannot bootstrap local service."
    assert env_message in result.stdout
    assert result.stdout.index(env_message) < result.stdout.index(docker_failure)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=local\n"
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_json_includes_env_action_when_docker_preflight_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose successful env seeding in Docker preflight failure payloads."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "DOCKER_DAEMON_UNREACHABLE"
    assert payload["env_action"] == "wrote_from_example"
    assert "env_error" not in payload
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=local\n"
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_json_includes_env_error_when_docker_preflight_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose env write failures even when Docker preflight exits early."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")
    _fail_path_write(monkeypatch, failing_path=".env")

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "DOCKER_DAEMON_UNREACHABLE"
    assert payload["env_error"] == {
        "operation": "write_env",
        "path": ".env",
        "env_file": ".env",
        "env_example": ".env.example",
        "message": "permission denied",
    }
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_json_includes_env_action_when_local_checks_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose successful env seeding when local checks fail before bootstrap."""
    from awf.service import doctor as doctor_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch)

    async def _fail_to_collect_doctor_report(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("doctor probe failed")

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _fail_to_collect_doctor_report)

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "BOOTSTRAP_LOCAL_CHECKS_FAILED"
    assert payload["message"] == "doctor probe failed"
    assert payload["env_action"] == "wrote_from_example"
    assert "env_error" not in payload
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=local\n"
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_warns_when_env_write_and_docker_preflight_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Warn about env write failures before reporting Docker preflight failure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")
    _fail_path_write(monkeypatch, failing_path=".env")

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    warning = "warning: could not write .env from .env.example: permission denied"
    docker_failure = "Docker is not available; cannot bootstrap local service."
    assert result.stdout.count(warning) == 1
    assert result.stdout.index(warning) < result.stdout.index(docker_failure)
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_fails_when_docker_diagnostic_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    bootstrap_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    report = _doctor_report()  # No diagnostics: no docker entry.

    async def _collect_doctor_report(_settings: object, **_kwargs: Any) -> Any:
        return report

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        bootstrap_calls.append({"settings": received_settings, **kwargs})
        return None

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1, result.output
    assert bootstrap_calls == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "DOCKER_DIAGNOSTIC_MISSING"
    assert not (tmp_path / "state").exists()


@pytest.mark.unit
def test_init_without_path_passes_strict_provider_options_to_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    captured = _stub_bootstrap_mode(monkeypatch)

    result = invoke_init_service_bootstrap(
        ["--provider", "github", "--provider", "opencode"],
    )

    assert result.exit_code == 0, result.output
    options = captured["bootstrap_calls"][0]["options"]
    assert options.strict_providers == frozenset({"github", "opencode"})


@pytest.mark.unit
def test_init_without_path_passes_skip_agent_runtime_build_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    captured = _stub_bootstrap_mode(monkeypatch)

    result = invoke_init_service_bootstrap(["--skip-agent-runtime-build"])

    assert result.exit_code == 0, result.output
    options = captured["bootstrap_calls"][0]["options"]
    assert options.skip_agent_runtime_build is True


@pytest.mark.unit
def test_init_without_path_handles_bootstrap_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service.bootstrap import ServiceBootstrapError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
        message="timed out waiting for local service readiness",
        last_status={"status": "fail", "checks": {"api": {"reason": "API_UNREACHABLE"}}},
    )
    _stub_bootstrap_mode(monkeypatch, bootstrap_error=error)

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_TIMEOUT"
    assert payload["last_status"]["status"] == "fail"
    combined = result.output
    assert "Traceback" not in combined


@pytest.mark.unit
def test_init_without_path_json_includes_env_error_when_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service.bootstrap import ServiceBootstrapError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_FAILED",
        message="docker compose failed",
        stage="compose_up",
        command=("docker", "compose", "up", "-d"),
        returncode=1,
        stderr="AWF_API_TOKEN is required",
    )
    _stub_bootstrap_mode(monkeypatch, bootstrap_error=error)
    _fail_path_write(monkeypatch, failing_path=".env")

    result = invoke_init_service_bootstrap(["--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_FAILED"
    assert payload["stage"] == "compose_up"
    assert payload["env_error"] == {
        "operation": "write_env",
        "path": ".env",
        "env_file": ".env",
        "env_example": ".env.example",
        "message": "permission denied",
    }
    assert "Traceback" not in result.output
