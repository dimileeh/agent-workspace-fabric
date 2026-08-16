"""Hosted validation env_file and worktree base-path tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedValidationDelegate
from awf.runtime.hosted_delegation_payloads import (
    _hosted_validation_profile_payload,
    _hosted_validation_rendered_stack_payload,
)
from tests.unit.runtime.test_hosted_validation_delegate import _config


@pytest.mark.unit
def test_hosted_validation_profile_payload_env_file_postgres_password_sets_trust(
    tmp_path: Path,
) -> None:
    """Password declared only via profile env_file must still inject trust.

    Rendered-stack sanitization already scans env_file; profile.services must
    match so hosted sidecar env stays consistent across both payload halves.
    """
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=env-file-only-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-pg-env-file-password",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "env_file": "postgres.env",
                    "environment": {"POSTGRES_USER": "awf"},
                }
            ],
        }
    )

    payload = _hosted_validation_profile_payload(profile, compose_dir=tmp_path)

    assert payload["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "env-file-only-secret" not in body


@pytest.mark.unit
async def test_hosted_run_profile_phases_resolves_env_file_from_worktree(
    tmp_path: Path,
) -> None:
    """Repo-relative profile env_file must scan the worktree, not compose dir.

    ``profile_services(..., base_path=worktree)`` resolves env_file from the
    worktree for rendered Compose. Hosted profile payload must use that same
    base so POSTGRES_PASSWORD is detected and trust is injected, while still
    sending ``worktree_path: null`` to Cloud.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "postgres.env").write_text(
        "POSTGRES_PASSWORD=worktree-env-file-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_dir = tmp_path / "compose-project"
    compose_dir.mkdir()
    compose_file = compose_dir / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-pg-worktree-env-file",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "env_file": "postgres.env",
                    "environment": {"POSTGRES_USER": "awf"},
                }
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path / "artifacts",
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=profile,
            phase_names=("validate",),
            worktree_path=worktree,
            include_coverage=False,
        )

    assert seen["body"]["worktree_path"] is None
    assert seen["body"]["profile"]["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(seen["body"], sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "worktree-env-file-secret" not in body


@pytest.mark.unit
async def test_hosted_probe_validate_command_tools_resolves_env_file_from_worktree(
    tmp_path: Path,
) -> None:
    """Toolchain probe must use the same worktree env_file base as phases.

    Probe omits credentials via omit_credential_env_keys; without profile_base_path
    a worktree-only POSTGRES_PASSWORD declaration is missed and trust is not
    injected, leaving the hosted sidecar unable to start passwordless.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "postgres.env").write_text(
        "POSTGRES_PASSWORD=worktree-probe-env-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_dir = tmp_path / "compose-project"
    compose_dir.mkdir()
    compose_file = compose_dir / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/probe_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/probe_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "probe_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "validate_toolchain_probe": {
                        "missing": [],
                        "probe_errored": False,
                        "probe_ran": True,
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-pg-probe-worktree-env-file",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "env_file": "postgres.env",
                    "environment": {"POSTGRES_USER": "awf"},
                }
            ],
            "phases": {"validate": ["ruff check src/awf"]},
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path / "artifacts",
            client=client,
        )
        await delegate.probe_validate_command_tools(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=profile,
            worktree_path=worktree,
        )

    assert seen["body"]["probe"] == "validate_toolchain"
    assert seen["body"]["profile"]["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(seen["body"], sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "worktree-probe-env-secret" not in body


@pytest.mark.unit
async def test_hosted_run_profile_coverage_resolves_env_file_from_worktree(
    tmp_path: Path,
) -> None:
    """Coverage-only hosted payload must use the same worktree env_file base as phases."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "postgres.env").write_text(
        "POSTGRES_PASSWORD=worktree-coverage-env-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_dir = tmp_path / "compose-project"
    compose_dir.mkdir()
    compose_file = compose_dir / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "coverage_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/coverage_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/coverage_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "coverage_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "coverage": {
                        "provider": "python",
                        "percent": 99.0,
                        "minimum_percent": 99.0,
                        "enforce": True,
                        "status": "passed",
                        "reason_code": "COVERAGE_OK",
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-pg-coverage-worktree-env-file",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "env_file": "postgres.env",
                    "environment": {"POSTGRES_USER": "awf"},
                }
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path / "artifacts",
            client=client,
        )
        await delegate.run_profile_coverage(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=profile,
            worktree_path=worktree,
        )

    assert seen["body"]["profile"]["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(seen["body"], sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "worktree-coverage-env-secret" not in body


@pytest.mark.unit
def test_rendered_stack_env_file_null_environment_still_injects_trust(
    tmp_path: Path,
) -> None:
    """Explicit environment: null must not block env_file-driven trust injection."""
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=null-env-file-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file:
      - postgres.env
    environment: null
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["postgres"]["environment"] == {
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "null-env-file-secret" not in body


@pytest.mark.unit
def test_rendered_stack_env_file_list_mapping_postgres_password_injects_trust(
    tmp_path: Path,
) -> None:
    """Mapping-shaped env_file entries declaring POSTGRES_PASSWORD get trust."""
    env_file = tmp_path / "db.env"
    env_file.write_text("POSTGRES_PASSWORD=mapping-env-file-secret\n", encoding="utf-8")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file:
      - path: db.env
        required: true
    environment:
      POSTGRES_USER: awf
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["postgres"]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "mapping-env-file-secret" not in body


@pytest.mark.unit
def test_rendered_stack_unreadable_env_file_does_not_inject_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unreadable env_file must not abort sanitization or invent trust mode.

    Force a reader failure instead of chmod(0o000): root bypasses mode bits, so
    a permission-only fixture can still parse POSTGRES_PASSWORD and inject trust.
    """
    env_file = tmp_path / "postgres.env"
    env_file.write_text("POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file: postgres.env
""".lstrip(),
        encoding="utf-8",
    )

    original_read_text = Path.read_text

    def _raise_for_env_file(self: Path, *args: object, **kwargs: object) -> str:
        if self == env_file:
            raise PermissionError(13, "Permission denied", str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_env_file)

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert "environment" not in payload["services"]["postgres"]


@pytest.mark.unit
def test_rendered_stack_resolves_env_file_from_worktree_base(
    tmp_path: Path,
) -> None:
    """Rendered stack must use the same worktree env_file base as profile.

    When compose lives outside the checkout and POSTGRES_PASSWORD is only in a
    worktree-relative env_file, compose_dir scanning misses it. Without
    env_file_base_path the rendered Postgres service omits the password without
    trust while profile.services (profile_base_path=worktree) injects trust —
    inconsistent sidecar env in the same hosted request.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "postgres.env").write_text(
        "POSTGRES_PASSWORD=worktree-rendered-stack-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_dir = tmp_path / "compose-project"
    compose_dir.mkdir()
    compose_file = compose_dir / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file: postgres.env
    environment:
      POSTGRES_USER: awf
""".lstrip(),
        encoding="utf-8",
    )

    without_base = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )
    assert without_base is not None
    assert without_base["services"]["postgres"]["environment"] == {"POSTGRES_USER": "awf"}

    with_base = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
        env_file_base_path=worktree,
    )
    assert with_base is not None
    assert with_base["services"]["postgres"]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(with_base, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "worktree-rendered-stack-secret" not in body


@pytest.mark.unit
async def test_hosted_run_profile_phases_rendered_stack_uses_worktree_env_file(
    tmp_path: Path,
) -> None:
    """Phase payload must keep profile and rendered_stack trust injection aligned."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "postgres.env").write_text(
        "POSTGRES_PASSWORD=worktree-stack-align-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_dir = tmp_path / "compose-project"
    compose_dir.mkdir()
    compose_file = compose_dir / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file: postgres.env
    environment:
      POSTGRES_USER: awf
""".lstrip(),
        encoding="utf-8",
    )

    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_align_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_align_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_align_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_align_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-pg-stack-worktree-env-file",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "env_file": "postgres.env",
                    "environment": {"POSTGRES_USER": "awf"},
                }
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path / "artifacts",
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            profile=profile,
            phase_names=("validate",),
            worktree_path=worktree,
            include_coverage=False,
        )

    expected_env = {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    assert seen["body"]["profile"]["services"][0]["environment"] == expected_env
    assert seen["body"]["rendered_stack"]["services"]["postgres"]["environment"] == expected_env
    body = json.dumps(seen["body"], sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "worktree-stack-align-secret" not in body
