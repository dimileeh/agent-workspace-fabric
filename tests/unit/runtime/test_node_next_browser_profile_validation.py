"""No-Docker validation-runner contract for the Node/browser profile fixture."""

from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolver
from awf.runtime.validation import HEALTHCHECK_OK, ValidationRunner

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "node_next_browser_app"
)
_COMPOSE_PROJECT = "awf_ws_node_browser"
_COMPOSE_FILE = Path("/fake/compose.yml")
_EXPECTED_BROWSER_VALIDATION = "browser validated awf-node-profile-fixture\n"


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "node browser workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile


class _TransientBrowserValidationHandler(BaseHTTPRequestHandler):
    failures_remaining = 1
    request_count = 0

    def do_GET(self) -> None:
        type(self).request_count += 1
        if self.path != "/validate":
            self._send("not found\n", status=404)
            return

        if type(self).failures_remaining > 0:
            type(self).failures_remaining -= 1
            self._send("browser validation failed: chromium still starting\n", status=500)
            return

        self._send(_EXPECTED_BROWSER_VALIDATION)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send(self, body: str, *, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.unit
async def test_node_next_browser_profile_setup_runs_setup_phase(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="setup ok\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_node_browser_setup",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("setup",),
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("setup", "01_setup.stdout")
    ]
    assert len(fake.calls) == 1
    assert "node scripts/setup.mjs" in fake.calls[0].args[-1]
    assert "validate-browser" not in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_node_next_browser_profile_healthchecks_are_opt_in(tmp_path: Path) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="browser validated awf-node-profile-fixture\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_node_browser_no_health",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("validate",),
        run_healthchecks=False,
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("validate", "01_validate.stdout")
    ]
    assert len(fake.calls) == 1
    assert "node scripts/validate-browser.mjs" in fake.calls[0].args[-1]
    assert "healthcheck.mjs" not in fake.calls[0].args[-1]


@pytest.mark.unit
async def test_node_next_browser_profile_healthchecks_precede_browser_validate(
    tmp_path: Path,
) -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="ok\n")
    fake.queue_result(returncode=0, stdout="ok\n")
    fake.queue_result(returncode=0, stdout="browser validated awf-node-profile-fixture\n")
    validator = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")

    result = await validator.run_profile_phases(
        workspace_id="ws_node_browser_validate",
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        profile=_load_profile(),
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert result.all_passed
    assert [(command.phase, command.stdout_path.name) for command in result.commands] == [
        ("healthcheck", "01_healthcheck.stdout"),
        ("healthcheck", "02_healthcheck.stdout"),
        ("validate", "01_validate.stdout"),
    ]
    assert [command.reason_code for command in result.commands[:2]] == [
        HEALTHCHECK_OK,
        HEALTHCHECK_OK,
    ]
    assert len(fake.calls) == 3
    assert "node scripts/healthcheck.mjs app" in fake.calls[0].args[-1]
    assert "node scripts/healthcheck.mjs browser" in fake.calls[1].args[-1]
    assert "node scripts/validate-browser.mjs" in fake.calls[2].args[-1]


@pytest.mark.unit
def test_browser_validate_script_retries_transient_validation_response() -> None:
    _TransientBrowserValidationHandler.failures_remaining = 1
    _TransientBrowserValidationHandler.request_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TransientBrowserValidationHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **os.environ,
            "BROWSER_VALIDATE_URL": f"http://127.0.0.1:{server.server_port}/validate",
            "BROWSER_VALIDATE_ATTEMPTS": "2",
            "BROWSER_VALIDATE_RETRY_DELAY_MS": "1",
        }
        completed = subprocess.run(
            ["node", str(_FIXTURE / "scripts" / "validate-browser.mjs")],
            check=True,
            capture_output=True,
            env=env,
            text=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert completed.stdout == _EXPECTED_BROWSER_VALIDATION
    assert "browser validation attempt 1 failed" in completed.stderr
    assert _TransientBrowserValidationHandler.request_count == 2


@pytest.mark.unit
def test_browser_validator_uses_ci_safe_chromium_launch_flags() -> None:
    validator = (_FIXTURE / "browser" / "validator-server.mjs").read_text(encoding="utf-8")

    assert "--no-sandbox" in validator
    assert "--disable-dev-shm-usage" in validator
