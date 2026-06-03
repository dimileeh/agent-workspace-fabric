"""``awf service gc`` thin-client tests.

The command is a thin trigger over ``POST /v1/service/gc``: the root
control-plane runs the deletion in-container. These tests assert the CLI maps
its flags to the request body, renders the response, and exits non-zero on a
``partial`` result -- they mock ``httpx.request`` rather than spinning an API.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from awf.cli.main import app
from awf.cli.service_commands import GCStatusFilter
from awf.db.enums import WorkspaceStatus

_runner = CliRunner()


def _mock_response(*, status_code: int = 200, payload: object = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None else b""
    response.text = json.dumps(payload) if payload is not None else ""
    response.json.return_value = payload
    response.request = httpx.Request("POST", "http://localhost:8000/v1/service/gc")
    return response


def _dry_run_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dry_run": True,
        "status": "dry_run",
        "reason_code": "CLEANUP_DRY_RUN",
        "candidate_count": 0,
        "preserved_count": 0,
        "deleted_path_count": 0,
        "total_estimated_bytes": 0,
        "candidates": [],
        "preserved": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_service_gc_defaults_to_json_dry_run_post() -> None:
    response = _mock_response(payload=_dry_run_payload(candidate_count=1))
    with patch("awf.cli.common.httpx.request", return_value=response) as mock:
        result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 0, result.output
    method, url = mock.call_args.args
    assert method == "POST"
    assert url.endswith("/v1/service/gc")
    assert mock.call_args.kwargs["json"] == {"execute": False}
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["candidate_count"] == 1


@pytest.mark.unit
def test_service_gc_maps_flags_to_request_body() -> None:
    response = _mock_response(
        payload={
            "dry_run": False,
            "status": "succeeded",
            "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        }
    )
    with patch("awf.cli.common.httpx.request", return_value=response) as mock:
        result = _runner.invoke(
            app,
            [
                "service",
                "gc",
                "--execute",
                "--min-age-hours",
                "12",
                "--limit",
                "5",
                "--status",
                "completed",
                "--exclude-status",
                "failed",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["json"] == {
        "execute": True,
        "min_age_hours": 12.0,
        "limit": 5,
        "statuses": ["completed"],
        "exclude_statuses": ["failed"],
    }


@pytest.mark.unit
def test_service_gc_can_target_superseded_status() -> None:
    """The thin client must drive the superseded-only cleanup path the API accepts.

    ``superseded`` is a terminal GC status but not a ``WorkspaceStatus`` enum
    member, so a bare ``list[WorkspaceStatus]`` option would reject it at parse
    time and leave the API's superseded path unreachable from the CLI.
    """
    response = _mock_response(
        payload={
            "dry_run": False,
            "status": "succeeded",
            "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        }
    )
    with patch("awf.cli.common.httpx.request", return_value=response) as mock:
        result = _runner.invoke(
            app,
            [
                "service",
                "gc",
                "--execute",
                "--status",
                "superseded",
                "--exclude-status",
                "superseded",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["json"] == {
        "execute": True,
        "statuses": ["superseded"],
        "exclude_statuses": ["superseded"],
    }


@pytest.mark.unit
def test_gc_status_filter_mirrors_workspace_status_plus_superseded() -> None:
    """``GCStatusFilter`` must stay in lockstep with the API's GC vocabulary."""
    assert {member.value for member in GCStatusFilter} == {
        member.value for member in WorkspaceStatus
    } | {"superseded"}


@pytest.mark.unit
def test_service_gc_uses_long_default_timeout() -> None:
    """Large ``--execute`` reclaims must not hit the 30s default request timeout."""
    response = _mock_response(
        payload={
            "dry_run": False,
            "status": "succeeded",
            "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        }
    )
    with patch("awf.cli.common.httpx.request", return_value=response) as mock:
        result = _runner.invoke(app, ["service", "gc", "--execute"])

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["timeout"] == 900.0


@pytest.mark.unit
def test_service_gc_honors_timeout_override() -> None:
    response = _mock_response(payload=_dry_run_payload())
    with patch("awf.cli.common.httpx.request", return_value=response) as mock:
        result = _runner.invoke(
            app,
            ["service", "gc", "--execute", "--timeout-seconds", "3600"],
        )

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["timeout"] == 3600.0


@pytest.mark.unit
def test_service_gc_partial_status_exits_nonzero() -> None:
    response = _mock_response(
        payload={
            "dry_run": False,
            "status": "partial",
            "reason_code": "CLEANUP_EXECUTION_PARTIAL",
            "delete_errors": [{"reason_code": "PATH_DELETE_PERMISSION_DENIED"}],
        }
    )
    with patch("awf.cli.common.httpx.request", return_value=response):
        result = _runner.invoke(app, ["service", "gc", "--execute"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["delete_errors"][0]["reason_code"] == "PATH_DELETE_PERMISSION_DENIED"


@pytest.mark.unit
def test_service_gc_http_error_exits_nonzero() -> None:
    response = _mock_response(
        status_code=401,
        payload={"detail": {"error_code": "UNAUTHORIZED", "message": "Invalid AWF API token."}},
    )
    with patch("awf.cli.common.httpx.request", return_value=response):
        result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 1
    assert "UNAUTHORIZED" in _combined_output(result)


@pytest.mark.unit
def test_service_gc_non_json_2xx_body_exits_clean() -> None:
    # A proxy / load balancer / early-close can return a 2xx with a non-JSON
    # body; the CLI must surface a clean error rather than letting
    # ``json.JSONDecodeError`` bubble out as a cryptic traceback.
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.content = b"<html>502 from upstream proxy</html>"
    response.text = "<html>502 from upstream proxy</html>"
    response.json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)
    response.request = httpx.Request("POST", "http://localhost:8000/v1/service/gc")
    with patch("awf.cli.common.httpx.request", return_value=response):
        result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 1
    assert "non-JSON response" in _combined_output(result)
    assert "502 from upstream proxy" in _combined_output(result)


@pytest.mark.unit
def test_service_gc_supports_pretty_output() -> None:
    response = _mock_response(
        payload={
            "dry_run": False,
            "status": "succeeded",
            "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
            "candidate_count": 1,
        }
    )
    with patch("awf.cli.common.httpx.request", return_value=response):
        result = _runner.invoke(app, ["service", "gc", "--execute", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "status: succeeded" in result.stdout
    assert "candidate_count: 1" in result.stdout


def _service_env_settings_stub(*, api_base_url: str, api_token: str | None) -> MagicMock:
    """Stub the resolved service settings the local ``.env`` would produce."""
    settings = MagicMock()
    settings.api_base_url = api_base_url
    settings.api_token = api_token
    return settings


def _clear_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWF_API_TOKEN", "AWF_BASE_URL", "AWF_CLI_BASE_URL", "AWF_API_HOST_PORT"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.unit
def test_service_gc_loads_service_env_token_and_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flags or process env, the compose ``.env`` token/URL are honoured.

    ``service gc`` is the only ``service_*`` command that triggers an
    authenticated REST call, so an ``AWF_API_TOKEN``/API port living only in
    ``docker/compose/.env`` must still reach the request -- matching sibling
    commands instead of silently 401-ing.
    """
    _clear_service_env(monkeypatch)
    settings = _service_env_settings_stub(
        api_base_url="http://localhost:9999", api_token="env-token"
    )
    response = _mock_response(payload=_dry_run_payload())
    with (
        patch("awf.service.config.local_service_environ", return_value={}),
        patch("awf.service.config.resolve_service_settings", return_value=settings),
        patch("awf.cli.common.httpx.request", return_value=response) as mock,
    ):
        result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 0, result.output
    _, url = mock.call_args.args
    assert url.startswith("http://localhost:9999/")
    assert mock.call_args.kwargs["headers"]["Authorization"] == "Bearer env-token"


@pytest.mark.unit
def test_service_gc_loads_service_env_base_url_when_token_in_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process-env token wins, but a missing base URL still falls back to ``.env``."""
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("AWF_API_TOKEN", "proc-token")
    settings = _service_env_settings_stub(
        api_base_url="http://localhost:9999", api_token="env-token"
    )
    response = _mock_response(payload=_dry_run_payload())
    with (
        patch("awf.service.config.local_service_environ", return_value={}),
        patch("awf.service.config.resolve_service_settings", return_value=settings),
        patch("awf.cli.common.httpx.request", return_value=response) as mock,
    ):
        result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 0, result.output
    _, url = mock.call_args.args
    assert url.startswith("http://localhost:9999/")
    assert mock.call_args.kwargs["headers"]["Authorization"] == "Bearer proc-token"


@pytest.mark.unit
def test_service_gc_loads_service_env_token_when_base_url_in_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process-env base URL wins, but a missing token still falls back to ``.env``."""
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("AWF_BASE_URL", "http://proc-host:1234")
    settings = _service_env_settings_stub(
        api_base_url="http://localhost:9999", api_token="env-token"
    )
    response = _mock_response(payload=_dry_run_payload())
    with (
        patch("awf.service.config.local_service_environ", return_value={}),
        patch("awf.service.config.resolve_service_settings", return_value=settings),
        patch("awf.cli.common.httpx.request", return_value=response) as mock,
    ):
        result = _runner.invoke(app, ["service", "gc"])

    assert result.exit_code == 0, result.output
    _, url = mock.call_args.args
    assert url.startswith("http://proc-host:1234/")
    assert mock.call_args.kwargs["headers"]["Authorization"] == "Bearer env-token"


@pytest.mark.unit
def test_service_gc_skips_service_env_when_overrides_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``--base-url``/``--api-token`` flags bypass the ``.env`` lookup entirely."""
    _clear_service_env(monkeypatch)
    response = _mock_response(payload=_dry_run_payload())
    with (
        patch("awf.service.config.resolve_service_settings") as resolve_settings,
        patch("awf.cli.common.httpx.request", return_value=response) as mock,
    ):
        result = _runner.invoke(
            app,
            [
                "service",
                "gc",
                "--base-url",
                "http://flag-host:4321",
                "--api-token",
                "flag-token",
            ],
        )

    assert result.exit_code == 0, result.output
    resolve_settings.assert_not_called()
    _, url = mock.call_args.args
    assert url.startswith("http://flag-host:4321/")
    assert mock.call_args.kwargs["headers"]["Authorization"] == "Bearer flag-token"


def _combined_output(result: Any) -> str:
    return f"{result.stdout}{getattr(result, 'stderr', '')}"
