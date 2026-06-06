"""CLI coverage for the ``awf start`` local-Core start wrapper (T05)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from typer.testing import CliRunner

from awf.cli import start_commands
from awf.cli.main import app
from awf.service.bootstrap import ServiceBootstrapError, ServiceBootstrapResult

_runner = CliRunner()

_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _success_result(
    *,
    status: str = "ok",
    docker: dict[str, object] | None = None,
    agent_readiness: dict[str, object] | None = None,
) -> ServiceBootstrapResult:
    return ServiceBootstrapResult(
        stages=(),
        service_status={
            "service": "awf",
            "status": status,
            "checks": {
                "docker": docker
                if docker is not None
                else {"ok": True, "status": "ok", "version": "27.0.1"}
            },
            "agent_readiness": agent_readiness
            if agent_readiness is not None
            else {"status": "ok", "providers": {"github": {"status": "ok"}}},
        },
    )


def _write_full_source_checkout(root: Path) -> Path:
    """Create every marker required by ``validate_source_checkout``."""
    (root / "src" / "awf").mkdir(parents=True)
    (root / "docker" / "compose").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "migrations").mkdir()
    files = {
        "pyproject.toml": "[project]\nname = 'awf'\n",
        "uv.lock": "",
        "alembic.ini": "[alembic]\n",
        ".env.example": "AWF_API_TOKEN=example\n",
        "openapi.json": "{}\n",
        "compose.yaml": "include:\n  - ./docker/compose/local-service.yml\n",
        "README.md": "# AWF\n",
        "RELEASING.md": "# Releasing\n",
        "src/awf/__init__.py": "",
        "docker/compose/local-service.yml": "services: {}\n",
        "docker/compose/workspace.base.yml.j2": "services: {}\n",
        "docker/agent-runtime.Dockerfile": "FROM scratch\n",
        "docker/control-plane.Dockerfile": "FROM scratch\n",
        "apps/console/Dockerfile": "FROM scratch\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _patch_start_wiring(
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: list[dict[str, object]],
    result: ServiceBootstrapResult | BaseException,
    settings: object | None = None,
    stored_config: object | None = None,
) -> None:
    """Patch every external seam the start command touches with fakes."""

    async def _fake_bootstrap(settings_arg: object, **kwargs: object) -> object:
        records.append({"settings": settings_arg, **kwargs})
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("awf.service.bootstrap.run_service_bootstrap", _fake_bootstrap)
    monkeypatch.setattr(
        "awf.cli.init_ops._resolve_service_compose_paths",
        lambda: (
            Path("/repo/compose.yaml"),
            Path("/repo/.env"),
            Path("/repo/.env.example"),
        ),
    )
    monkeypatch.setattr(
        "awf.cli.init_ops._resolve_service_runtime_env_files",
        lambda *_a, **_k: (Path("/repo/.env"), Path("/repo/.env")),
    )
    monkeypatch.setattr("awf.service.config.local_service_environ", lambda **_k: {})
    resolved_settings = settings or SimpleNamespace(
        api_base_url="http://localhost:8000",
        console_url=None,
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda *_a, **_k: resolved_settings,
    )
    monkeypatch.setattr("awf.common.config.Settings", lambda **_k: object())
    monkeypatch.setattr(
        "awf.host_setup.config.read_host_setup_config",
        lambda **_k: (
            stored_config if stored_config is not None else SimpleNamespace(source_checkout=None)
        ),
    )


# --------------------------------------------------------------------------- #
# 1. Parser / help / dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_start_help_advertises_options_and_local_core() -> None:
    """Help advertises every public flag and the local-Core purpose."""
    result = _runner.invoke(app, ["start", "--help"], env={"COLUMNS": "200"})
    visible = click.unstyle(result.output)
    # Rich wraps help prose, so collapse whitespace before substring checks.
    collapsed = " ".join(visible.split())

    assert result.exit_code == 0, result.output
    assert "Start local AWF Core" in collapsed
    for flag in (
        "--rebuild",
        "--skip-agent-runtime-build",
        "--headless",
        "--console-port",
        "--timeout-seconds",
        "--source-checkout",
        "--format",
    ):
        assert flag in visible, flag
    assert "awf service bootstrap" in collapsed
    assert "Traceback" not in visible


@pytest.mark.unit
def test_start_rebuild_conflicts_with_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--rebuild` plus `--skip-agent-runtime-build` exits 2 without bootstrapping."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(app, ["start", "--rebuild", "--skip-agent-runtime-build"])

    assert result.exit_code == 2
    assert "--rebuild" in result.stderr
    assert "--skip-agent-runtime-build" in result.stderr
    assert records == []


# --------------------------------------------------------------------------- #
# 2. Bootstrap delegation (fake bootstrap runner)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    (
        ([], {"skip_agent_runtime_build": False, "force_rebuild": False}),
        (
            ["--skip-agent-runtime-build"],
            {"skip_agent_runtime_build": True, "force_rebuild": False},
        ),
        (["--rebuild"], {"skip_agent_runtime_build": False, "force_rebuild": True}),
    ),
)
def test_start_maps_flags_to_bootstrap_options(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    expected: dict[str, bool],
) -> None:
    """Build flags map onto ServiceBootstrapOptions exactly."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(app, ["start", "--format", "json", "--timeout-seconds", "12", *args])

    assert result.exit_code == 0, result.output
    options = records[0]["options"]
    assert options.skip_agent_runtime_build is expected["skip_agent_runtime_build"]
    assert options.force_rebuild is expected["force_rebuild"]
    assert options.start_console is True
    assert options.timeout_seconds == 12
    # Default discovery: no explicit asset root.
    assert records[0]["asset_root"] is None


@pytest.mark.unit
def test_start_headless_skips_console_bootstrap_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--headless` does not start the console or print console-open guidance."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(app, ["start", "--headless", "--format", "json"])

    assert result.exit_code == 0, result.output
    options = records[0]["options"]
    assert options.start_console is False
    payload = json.loads(result.stdout)
    assert "console_url" not in payload["details"]
    assert all("Open the console" not in step for step in payload["next_steps"])


@pytest.mark.unit
def test_start_console_port_override_updates_bootstrap_env_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--console-port` publishes the console on that host port and reports it."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    def _resolve_settings(_base: object, *, environ: dict[str, str]) -> object:
        return SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url=f"http://localhost:{environ['AWF_CONSOLE_HOST_PORT']}",
        )

    monkeypatch.setattr("awf.service.config.resolve_service_settings", _resolve_settings)

    result = _runner.invoke(app, ["start", "--console-port", "3333", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert records[0]["service_environ"]["AWF_CONSOLE_HOST_PORT"] == "3333"
    assert records[0]["options"].start_console is True
    payload = json.loads(result.stdout)
    assert payload["details"]["console_url"] == "http://127.0.0.1:3333"


@pytest.mark.unit
def test_start_headless_conflicts_with_console_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--headless` plus `--console-port` exits 2 without bootstrapping."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(app, ["start", "--headless", "--console-port", "3333"])

    assert result.exit_code == 2
    assert "--headless" in result.stderr
    assert "--console-port" in result.stderr
    assert records == []


@pytest.mark.unit
def test_start_success_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Success JSON carries URLs, docker/provider summary, and health; exit 0."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(
        monkeypatch,
        records=records,
        result=_success_result(),
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url="http://localhost:3000",
        ),
    )

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["command"] == "awf start"
    details = payload["details"]
    assert details["api_url"] == "http://127.0.0.1:8000"
    assert details["console_url"] == "http://127.0.0.1:3000"
    assert details["docker"]["version"] == "27.0.1"
    assert details["providers"]["status"] == "ok"
    assert details["providers"]["providers"]["github"] == "ok"
    assert details["health"] == "ok"
    assert payload["next_steps"]
    # First-run output leads with the provider-free local health proof (T10).
    assert "awf smoke run --mocked-local" in payload["next_steps"][0]


@pytest.mark.unit
def test_start_success_pretty_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretty success renders a readable panel with next commands to stdout."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(app, ["start"])

    assert result.exit_code == 0, result.output
    assert "Status: success" in result.stdout
    assert "Command: awf start" in result.stdout
    assert "127.0.0.1" in result.stdout
    assert "awf init" in result.stdout
    assert "Traceback" not in result.stdout


# --------------------------------------------------------------------------- #
# 3. Source asset selection
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_start_with_valid_source_checkout_pins_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A verified `--source-checkout` pins the asset root and compose file."""
    checkout = _write_full_source_checkout(tmp_path / "checkout")
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(
        app,
        ["start", "--format", "json", "--source-checkout", str(checkout)],
    )

    assert result.exit_code == 0, result.output
    assert records[0]["asset_root"] == checkout.resolve()
    assert records[0]["compose_file"] == checkout.resolve() / "compose.yaml"


@pytest.mark.unit
def test_resolve_start_inputs_source_checkout_falls_back_to_root_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verified source checkout reads and forwards root .env to Docker Compose."""
    root = (tmp_path / "checkout").resolve()
    (root / "docker" / "compose").mkdir(parents=True)
    root_env = root / ".env"
    root_env.write_text("AWF_API_TOKEN=from-root\n", encoding="utf-8")
    # Intentionally no legacy compose env: root .env is the active service env.

    verified = SimpleNamespace(
        root=root,
        compose_file=root / "compose.yaml",
    )

    captured: dict[str, object] = {}

    def _capture_environ(**kwargs: object) -> dict[str, str]:
        captured["environ_env_file"] = kwargs.get("env_file")
        return {}

    def _capture_settings(**kwargs: object) -> object:
        captured["settings_env_file"] = kwargs.get("_env_file")
        return object()

    monkeypatch.setattr("awf.service.config.local_service_environ", _capture_environ)
    monkeypatch.setattr("awf.common.config.Settings", _capture_settings)
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda *_a, **_k: SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
    )

    inputs = start_commands._resolve_start_bootstrap_inputs(verified)

    assert captured["environ_env_file"] == root_env
    assert captured["settings_env_file"] == root_env
    assert inputs.compose_env_file == root_env


@pytest.mark.unit
def test_start_with_invalid_source_checkout_fails_without_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A checkout missing markers fails loudly and never starts Core."""
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(
        app,
        ["start", "--format", "json", "--source-checkout", str(incomplete)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SOURCE_CHECKOUT_INVALID"
    assert payload["issues"][0]["details"]["missing_markers"]
    assert records == []


@pytest.mark.unit
def test_start_stale_stored_metadata_fails_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale stored source metadata fails loudly; no silent package fallback."""
    records: list[dict[str, object]] = []
    stored = SimpleNamespace(source_checkout=SimpleNamespace(root=Path("/gone")))
    _patch_start_wiring(
        monkeypatch,
        records=records,
        result=_success_result(),
        stored_config=stored,
    )

    def _raise_stale(_metadata: object, **_kwargs: object) -> object:
        from awf.host_setup.source_assets import (
            SOURCE_CHECKOUT_ASSETS_STALE,
            SourceCheckoutError,
        )

        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
            message="stale",
            root=Path("/gone"),
            missing_markers=("pyproject.toml",),
        )

    monkeypatch.setattr(
        "awf.host_setup.source_assets.verified_source_from_metadata",
        _raise_stale,
    )

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SOURCE_CHECKOUT_ASSETS_STALE"
    assert records == []


@pytest.mark.unit
def test_start_no_stored_metadata_uses_default_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No source selection falls through to default discovery (asset_root=None)."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert records[0]["asset_root"] is None


@pytest.mark.unit
def test_start_tolerates_corrupt_config_without_source_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt config is tolerated as 'no source selection' for default start."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(monkeypatch, records=records, result=_success_result())

    def _raise_corrupt(**_kwargs: object) -> object:
        from awf.host_setup.config import HOST_SETUP_CONFIG_CORRUPT, HostSetupConfigError

        raise HostSetupConfigError(
            reason_code=HOST_SETUP_CONFIG_CORRUPT,
            message="corrupt",
            path=Path("/home/x/.awf/config.yml"),
        )

    monkeypatch.setattr("awf.host_setup.config.read_host_setup_config", _raise_corrupt)

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert records[0]["asset_root"] is None


# --------------------------------------------------------------------------- #
# 4. Structured bootstrap failure preservation
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_start_migration_failure_preserves_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migrate stage failure maps to START_MIGRATION_FAILED with diagnostics."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
        message="migrate failed with exit code 1",
        stage="migrate",
        command=("docker", "compose", "up", "migrate"),
        returncode=1,
        stderr="alembic.util.exc.CommandError: target database is out of date",
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "START_MIGRATION_FAILED"
    bootstrap = payload["issues"][0]["details"]["bootstrap"]
    assert bootstrap["stage"] == "migrate"
    assert bootstrap["returncode"] == 1
    assert "out of date" in bootstrap["stderr"]


@pytest.mark.unit
def test_start_port_conflict_maps_reason_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A port-bind stage failure maps to START_PORT_CONFLICT."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
        message="postgres failed with exit code 1",
        stage="postgres",
        returncode=1,
        stderr="Error: Bind for 0.0.0.0:8000 failed: port is already allocated",
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "START_PORT_CONFLICT"
    details = payload["issues"][0]["details"]
    assert details["bootstrap"]["stage"] == "postgres"
    assert details["port"] == 8000


@pytest.mark.unit
def test_start_timeout_maps_to_health_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bootstrap timeout maps to START_HEALTH_TIMEOUT and keeps last_status."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
        message="timed out waiting for local service readiness",
        last_status={"status": "fail", "checks": {"api": {"reason": "API_UNREACHABLE"}}},
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "START_HEALTH_TIMEOUT"
    bootstrap = payload["issues"][0]["details"]["bootstrap"]
    assert bootstrap["last_status"]["checks"]["api"]["reason"] == "API_UNREACHABLE"


@pytest.mark.unit
def test_start_assets_not_found_maps_to_compose_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing bootstrap assets map to START_COMPOSE_ASSETS_MISSING."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND",
        message="Cannot resolve AWF bootstrap assets for local service startup.",
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "START_COMPOSE_ASSETS_MISSING"


@pytest.mark.unit
def test_start_unclassified_stage_failure_echoes_reason_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unclassified stage failure echoes the bootstrap reason code intact."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
        message="api_worker failed with exit code 3",
        stage="api_worker",
        returncode=3,
        stderr="container exited unexpectedly",
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_STAGE_FAILED"
    bootstrap = payload["issues"][0]["details"]["bootstrap"]
    assert bootstrap["stage"] == "api_worker"
    assert bootstrap["returncode"] == 3


@pytest.mark.unit
def test_start_failure_pretty_goes_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretty failure output goes to stderr with exit 1, stdout empty."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
        message="timed out",
        last_status={"status": "fail"},
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    result = _runner.invoke(app, ["start"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Status: failed" in result.stderr
    assert "START_HEALTH_TIMEOUT" in result.stderr


@pytest.mark.unit
def test_start_redacts_tokens_in_failure_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token-shaped value in bootstrap stderr is redacted in JSON and pretty."""
    records: list[dict[str, object]] = []
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
        message="api_worker failed",
        stage="api_worker",
        returncode=1,
        stderr=f"leaked token {_TOKEN} in worker logs",
    )
    _patch_start_wiring(monkeypatch, records=records, result=error)

    json_result = _runner.invoke(app, ["start", "--format", "json"])
    assert json_result.exit_code == 1
    assert _TOKEN not in json_result.stdout

    pretty_result = _runner.invoke(app, ["start"])
    assert pretty_result.exit_code == 1
    assert _TOKEN not in pretty_result.stderr


@pytest.mark.unit
def test_start_keyboard_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    """A KeyboardInterrupt during bootstrap exits 130."""
    records: list[dict[str, object]] = []
    _patch_start_wiring(
        monkeypatch,
        records=records,
        result=KeyboardInterrupt(),
    )

    result = _runner.invoke(app, ["start", "--format", "json"])

    assert result.exit_code == 130


# --------------------------------------------------------------------------- #
# 5. Pure helper unit tests (no CLI)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_start_success_payload_normalizes_local_urls() -> None:
    """The success payload normalizes localhost hosts to 127.0.0.1."""
    settings = SimpleNamespace(
        api_base_url="http://localhost:8000",
        console_url=None,
    )
    payload = start_commands._start_success_payload(settings, _success_result())  # noqa: SLF001

    assert payload.status == "success"
    assert payload.details["api_url"] == "http://127.0.0.1:8000"
    assert payload.details["console_url"] == "http://127.0.0.1:3000"
    assert payload.details["health"] == "ok"
    assert payload.details["docker"]["ok"] is True


@pytest.mark.unit
def test_start_success_payload_preserves_remote_console_url() -> None:
    """A configured non-local console URL is preserved unchanged."""
    settings = SimpleNamespace(
        api_base_url="http://0.0.0.0:8000",
        console_url="https://console.example.com",
    )
    payload = start_commands._start_success_payload(settings, _success_result())  # noqa: SLF001

    assert payload.details["api_url"] == "http://127.0.0.1:8000"
    assert payload.details["console_url"] == "https://console.example.com"


@pytest.mark.unit
def test_start_success_payload_leads_with_provider_free_proof() -> None:
    """First-run next steps lead with the no-token local health proof (T10).

    A skeptical evaluator must be able to prove local Core health right after
    ``awf start`` without handing AWF GitHub/PR authority or a provider token, so
    the leading next step is the provider-free ``awf smoke run --mocked-local``
    proof and it must not ask for any token or secret.
    """
    settings = SimpleNamespace(
        api_base_url="http://localhost:8000",
        console_url=None,
    )
    payload = start_commands._start_success_payload(settings, _success_result())  # noqa: SLF001

    next_steps = payload.next_steps
    assert next_steps[0].startswith("Run awf smoke run --mocked-local")
    assert "token" not in next_steps[0].lower()
    assert "secret" not in next_steps[0].lower()
    # The existing onboarding/console guidance is preserved after the proof.
    joined = "\n".join(next_steps)
    assert "awf init" in joined


@pytest.mark.unit
def test_env_migration_details_ignores_non_callable_to_dict() -> None:
    """Env migration metadata is included only for callable to_dict providers."""
    migration = SimpleNamespace(to_dict={"path": ".env"})

    assert start_commands._env_migration_details(migration) is None  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (
            ServiceBootstrapError(
                reason_code="SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND",
                message="missing assets",
            ),
            "START_COMPOSE_ASSETS_MISSING",
        ),
        (
            ServiceBootstrapError(
                reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
                message="timed out",
                last_status={"status": "fail"},
            ),
            "START_HEALTH_TIMEOUT",
        ),
        (
            ServiceBootstrapError(
                reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
                message="migrate failed",
                stage="migrate",
                returncode=1,
            ),
            "START_MIGRATION_FAILED",
        ),
        (
            ServiceBootstrapError(
                reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
                message="postgres failed",
                stage="postgres",
                returncode=1,
                stderr="bind: address already in use",
            ),
            "START_PORT_CONFLICT",
        ),
        (
            ServiceBootstrapError(
                reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
                message="api_worker failed",
                stage="api_worker",
                returncode=2,
                stderr="boom",
            ),
            "SERVICE_BOOTSTRAP_STAGE_FAILED",
        ),
    ),
)
def test_start_failure_payload_classifies_and_preserves(
    error: ServiceBootstrapError,
    expected: str,
) -> None:
    """The failure helper classifies reason codes and embeds the diagnostic."""
    payload = start_commands._start_failure_payload(error)  # noqa: SLF001

    assert payload.status == "failed"
    assert payload.reason_code == expected
    assert payload.issues[0].details["bootstrap"] == error.to_dict()


# --------------------------------------------------------------------------- #
# 6. Helper branch coverage (classification, source-checkout, summaries)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_classify_returns_none_for_unrecognized_reason_code() -> None:
    """A bootstrap reason code outside the known set stays unclassified."""
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_DOCKER_UNAVAILABLE",
        message="docker daemon is unreachable",
    )

    assert start_commands._classify_start_failure(error) is None  # noqa: SLF001


@pytest.mark.unit
def test_unclassified_failure_without_stage_uses_generic_cause() -> None:
    """An unclassified, stage-less failure echoes the reason code with a generic cause."""
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_DOCKER_UNAVAILABLE",
        message="docker daemon is unreachable",
    )

    payload = start_commands._start_failure_payload(error)  # noqa: SLF001

    assert payload.status == "failed"
    assert payload.reason_code == "SERVICE_BOOTSTRAP_DOCKER_UNAVAILABLE"
    issue = payload.issues[0]
    assert issue.remediation.cause == "Local Core bootstrap failed before completing startup."
    assert issue.details["bootstrap"] == error.to_dict()


@pytest.mark.unit
def test_port_conflict_without_stage_omits_stage_detail() -> None:
    """A stage-less port-bind failure still classifies and surfaces the port only."""
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_STAGE_FAILED",
        message="compose up failed",
        stderr="Error: bind for 0.0.0.0:8000 failed: port is already allocated",
    )

    payload = start_commands._start_failure_payload(error)  # noqa: SLF001

    assert payload.reason_code == "START_PORT_CONFLICT"
    details = payload.issues[0].details
    assert "stage" not in details
    assert details["port"] == 8000


@pytest.mark.unit
def test_source_checkout_failure_without_markers_embeds_details() -> None:
    """A stale checkout with detail metadata but no missing markers preserves details."""
    from awf.host_setup.source_assets import SOURCE_CHECKOUT_ASSETS_STALE, SourceCheckoutError

    error = SourceCheckoutError(
        reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
        message="source checkout assets are stale",
        root=Path("/srv/awf"),
        details={"expected_digest": "abc123", "actual_digest": "def456"},
    )

    payload = start_commands._source_checkout_failure_payload(error)  # noqa: SLF001

    assert payload.reason_code == "SOURCE_CHECKOUT_ASSETS_STALE"
    details = payload.issues[0].details
    assert "missing_markers" not in details
    assert details["source_checkout"] == {
        "expected_digest": "abc123",
        "actual_digest": "def456",
    }
    assert details["root"] == "/srv/awf"


@pytest.mark.unit
def test_normalize_local_url_returns_input_on_invalid_url() -> None:
    """An unparseable URL is returned unchanged rather than raising."""
    assert start_commands._normalize_local_url("http://[::1") == "http://[::1"  # noqa: SLF001


@pytest.mark.unit
def test_normalize_local_url_returns_input_on_invalid_port() -> None:
    """A local-alias host with a non-numeric port is returned unchanged, not raising.

    ``urlsplit`` accepts a malformed port lazily; ``parsed.port`` only raises on
    access, so the guard must treat it the same as an unparseable URL.
    """
    assert (
        start_commands._normalize_local_url("http://localhost:bad")  # noqa: SLF001
        == "http://localhost:bad"
    )


@pytest.mark.unit
def test_normalize_local_url_handles_local_host_without_port() -> None:
    """A local-alias host without a port is rewritten to the loopback display host."""
    assert (
        start_commands._normalize_local_url("http://localhost/v1")  # noqa: SLF001
        == "http://127.0.0.1/v1"
    )


@pytest.mark.unit
def test_normalize_local_url_rewrites_ipv6_loopback() -> None:
    """An IPv6 loopback host is normalized to the 127.0.0.1 display host."""
    assert (
        start_commands._normalize_local_url("http://[::1]:8000")  # noqa: SLF001
        == "http://127.0.0.1:8000"
    )


@pytest.mark.unit
def test_docker_summary_unknown_for_non_mapping() -> None:
    """A non-mapping docker check degrades to an unknown-status summary."""
    assert start_commands._docker_summary(None) == {"status": "unknown"}  # noqa: SLF001


@pytest.mark.unit
def test_docker_summary_includes_reason_and_skips_absent_version() -> None:
    """The docker summary surfaces a reason and omits a missing version."""
    summary = start_commands._docker_summary(  # noqa: SLF001
        {"ok": False, "status": "degraded", "reason": "daemon slow"}
    )

    assert summary == {"ok": False, "status": "degraded", "reason": "daemon slow"}


@pytest.mark.unit
def test_providers_summary_unknown_for_non_mapping() -> None:
    """A non-mapping agent-readiness payload degrades to an unknown-status summary."""
    assert start_commands._providers_summary(None) == {"status": "unknown"}  # noqa: SLF001


@pytest.mark.unit
def test_providers_summary_skips_non_mapping_providers() -> None:
    """Provider summary omits the providers map when it is not a mapping."""
    summary = start_commands._providers_summary(  # noqa: SLF001
        {"status": "ready", "providers": None}
    )

    assert summary == {"status": "ready"}


# --------------------------------------------------------------------------- #
# 7. Packaged-asset selection when no source checkout is configured (T13, AC2)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_resolve_start_source_checkout_none_without_stored_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recorded source checkout resolves to None (default/packaged discovery)."""
    monkeypatch.setattr(
        "awf.host_setup.config.read_host_setup_config",
        lambda **_k: SimpleNamespace(source_checkout=None),
    )

    assert start_commands._resolve_start_source_checkout(None) is None  # noqa: SLF001


@pytest.mark.unit
def test_service_compose_paths_resolve_from_packaged_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no source checkout, compose paths come from the packaged bootstrap root.

    This is the asset-location contract behind ``awf start`` for a clean install:
    ``_resolve_service_compose_paths`` selects the packaged ``bootstrap_assets``
    compose file and the package-relative ``.env``/`.env.example` seeds.
    """
    from awf.cli import init_ops
    from awf.service import bootstrap as bootstrap_mod

    packaged_root = (tmp_path / "site-packages" / "awf" / "bootstrap_assets").resolve()
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: packaged_root)
    monkeypatch.setattr(
        bootstrap_mod,
        "is_packaged_bootstrap_asset_root",
        lambda candidate: candidate.resolve() == packaged_root,
    )

    compose_file, env_file, env_example = init_ops._resolve_service_compose_paths()  # noqa: SLF001

    assert compose_file == packaged_root / "compose.yaml"
    assert env_file == Path(".env")
    assert env_example == packaged_root / ".env.example"


@pytest.mark.unit
def test_resolve_start_inputs_none_delegates_to_packaged_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Start with no source checkout pins no asset root and uses discovered compose paths.

    ``asset_root is None`` lets ``run_service_bootstrap`` run its own discovery,
    which includes the packaged fallback, while the compose file is sourced from
    ``_resolve_service_compose_paths`` (the packaged-root selector above).
    """
    packaged_compose = tmp_path / "awf" / "bootstrap_assets" / "compose.yaml"
    monkeypatch.setattr(
        "awf.cli.init_ops._resolve_service_compose_paths",
        lambda: (packaged_compose, Path(".env"), packaged_compose.parent / ".env.example"),
    )
    monkeypatch.setattr(
        "awf.cli.init_ops._resolve_service_runtime_env_files",
        lambda *_a, **_k: (Path(".env"), Path(".env")),
    )
    monkeypatch.setattr("awf.service.config.local_service_environ", lambda **_k: {})
    monkeypatch.setattr("awf.common.config.Settings", lambda **_k: object())
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda *_a, **_k: SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
    )

    inputs = start_commands._resolve_start_bootstrap_inputs(None)  # noqa: SLF001

    assert inputs.asset_root is None
    assert inputs.compose_file == packaged_compose
    assert inputs.compose_env_file == Path(".env")
