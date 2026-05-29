"""Regression tests for the Node browser validation fixture."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import textwrap
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "node_next_browser_app"
)
_VALIDATOR_SERVER = _FIXTURE_ROOT / "browser" / "validator-server.mjs"
_BROWSER_DOCKERFILE = _FIXTURE_ROOT / "Dockerfile.playwright"
_HEALTHCHECK_PROCESS_TIMEOUT_SECONDS = 10
_VALIDATOR_START_ATTEMPTS = 10

pytestmark = pytest.mark.unit


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_healthz(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/healthz"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"validator server exited early with {process.returncode}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                assert response.read().decode("utf-8") == "ok\n"
                return
        except Exception as error:  # pragma: no cover - diagnostic retry loop
            last_error = error
            time.sleep(0.05)
    raise AssertionError(f"validator server did not become healthy: {last_error!r}")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def _start_validator_server(
    validator_server: Path,
    env: dict[str, str],
) -> tuple[int, subprocess.Popen[str]]:
    last_error: AssertionError | None = None
    for _ in range(_VALIDATOR_START_ATTEMPTS):
        port = _free_port()
        process = subprocess.Popen(
            ["node", str(validator_server)],
            env={**env, "PORT": str(port)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_healthz(port, process)
            return port, process
        except AssertionError as error:
            _terminate_process(process)
            last_error = error
            message = str(error)
            if "EADDRINUSE" not in message and "address already in use" not in message:
                raise
            time.sleep(0.05)

    raise AssertionError(
        f"validator server could not bind an available port after "
        f"{_VALIDATOR_START_ATTEMPTS} attempts"
    ) from last_error


def _write_playwright_stub(node_modules: Path) -> Path:
    package_dir = node_modules / "playwright-core"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        '{"type":"module","main":"index.js"}',
        encoding="utf-8",
    )
    (package_dir / "index.js").write_text(
        textwrap.dedent(
            """
            import { appendFileSync } from "node:fs";

            const page = {
              async goto() {},
              async waitForSelector() {},
              async textContent() {
                return "AWF Node Profile Fixture";
              },
              async evaluate() {
                return {
                  id: "awf-node-profile-fixture",
                  ready: true,
                  runtime: "node-next-browser-app",
                };
              },
            };

            function delay(ms) {
              return new Promise((resolve) => setTimeout(resolve, ms));
            }

            export const chromium = {
              async launch(options = {}) {
                appendFileSync(process.env.PLAYWRIGHT_STUB_LOG, "launch\\n", "utf8");
                if (process.env.PLAYWRIGHT_STUB_OPTIONS_LOG) {
                  appendFileSync(
                    process.env.PLAYWRIGHT_STUB_OPTIONS_LOG,
                    `${JSON.stringify(options)}\\n`,
                    "utf8",
                  );
                }
                await delay(100);
                return {
                  isConnected() {
                    return true;
                  },
                  async newPage() {
                    return page;
                  },
                  async newContext() {
                    return {
                      async newPage() {
                        return page;
                      },
                      async close() {},
                    };
                  },
                  async close() {},
                };
              },
            };
            """
        ).strip(),
        encoding="utf-8",
    )
    return package_dir


def test_browser_sidecar_dockerfile_uses_distro_chromium_contract() -> None:
    """The Docker smoke fixture should not depend on MCR Playwright image pulls."""
    dockerfile = _BROWSER_DOCKERFILE.read_text(encoding="utf-8")

    assert "mcr.microsoft.com/playwright" not in dockerfile
    assert "FROM node:22-bookworm-slim" in dockerfile
    assert "apt-get install" in dockerfile
    assert "chromium" in dockerfile
    assert "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium" in dockerfile


def test_browser_sidecar_dockerfile_runs_validator_as_non_root_user() -> None:
    """The browser validator fixture should not run as root."""
    dockerfile = _BROWSER_DOCKERFILE.read_text(encoding="utf-8")

    assert "useradd --create-home --uid 10001 awf" in dockerfile
    assert "mkdir -p /home/awf/.cache /home/awf/.config" in dockerfile
    assert "chown -R awf:awf /app /home/awf" in dockerfile
    assert "chown -R awf:awf /app" in dockerfile
    assert "COPY --chown=awf:awf browser/validator-server.mjs" in dockerfile
    assert "COPY --chown=awf:awf scripts/container-healthcheck.mjs" in dockerfile
    assert "ENV HOME=/home/awf" in dockerfile
    assert "ENV XDG_CACHE_HOME=/home/awf/.cache" in dockerfile
    assert "ENV XDG_CONFIG_HOME=/home/awf/.config" in dockerfile
    assert "USER awf" in dockerfile
    assert dockerfile.index("USER awf") < dockerfile.index(
        'CMD ["node", "/app/browser/validator-server.mjs"]'
    )


class _HangingHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(30)

    def log_message(self, format: str, *args: object) -> None:
        return


class _TrailingWhitespaceHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\r\n  ")

    def log_message(self, format: str, *args: object) -> None:
        return


class _SlowTrailingWhitespaceHealthHandler(_TrailingWhitespaceHealthHandler):
    def do_GET(self) -> None:
        time.sleep(2.25)
        super().do_GET()


class _HangingHealthServer(ThreadingHTTPServer):
    daemon_threads = True


def _run_healthcheck(
    *,
    port: int,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["node", "scripts/healthcheck.mjs", "app"],
        cwd=_FIXTURE_ROOT,
        env={
            **os.environ,
            "APP_BASE_URL": f"http://127.0.0.1:{port}",
            **(extra_env or {}),
        },
        capture_output=True,
        timeout=_HEALTHCHECK_PROCESS_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_healthcheck_accepts_trimmed_ok_response() -> None:
    """Health endpoint checks should tolerate benign trailing whitespace."""
    server = _HangingHealthServer(("127.0.0.1", 0), _TrailingWhitespaceHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        port = server.server_address[1]
        result = _run_healthcheck(port=port)
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0
    assert result.stdout == b"ok\r\n  "


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_healthcheck_process_timeout_allows_script_fetch_budget() -> None:
    """The test harness should not kill healthchecks before their own timeout."""
    server = _HangingHealthServer(("127.0.0.1", 0), _SlowTrailingWhitespaceHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        port = server.server_address[1]
        result = _run_healthcheck(port=port)
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0
    assert result.stdout == b"ok\r\n  "


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_healthcheck_fetch_times_out_when_service_hangs() -> None:
    """A hung health endpoint should fail inside the script-level fetch timeout."""
    server = _HangingHealthServer(("127.0.0.1", 0), _HangingHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        port = server.server_address[1]
        result = _run_healthcheck(
            port=port,
            extra_env={"AWF_HEALTHCHECK_FETCH_TIMEOUT_MS": "50"},
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode != 0
    assert b"timed out fetching app health response after 50ms" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_validator_server_reuses_browser_for_concurrent_validation_requests(
    tmp_path: Path,
) -> None:
    """Concurrent validations should share one browser process."""
    launch_log = tmp_path / "launches.log"
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()
    validator_server = browser_dir / "validator-server.mjs"
    validator_server.write_text(_VALIDATOR_SERVER.read_text(encoding="utf-8"), encoding="utf-8")
    _write_playwright_stub(tmp_path / "node_modules")

    env = {
        **os.environ,
        "APP_BASE_URL": "http://fixture-app.invalid",
        "AWF_VALIDATOR_HOST": "127.0.0.1",
        "PLAYWRIGHT_STUB_LOG": str(launch_log),
    }
    port, process = _start_validator_server(validator_server, env)

    try:
        validate_url = f"http://127.0.0.1:{port}/validate"

        def validate() -> str:
            with urllib.request.urlopen(validate_url, timeout=10) as response:
                return response.read().decode("utf-8")

        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(lambda _: validate(), range(6)))

        assert responses == ["browser validated awf-node-profile-fixture\n"] * 6
        assert launch_log.read_text(encoding="utf-8").splitlines() == ["launch"]
    finally:
        _terminate_process(process)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_validator_server_uses_configured_chromium_executable_path(
    tmp_path: Path,
) -> None:
    """The fixture image supplies distro Chromium instead of bundled browsers."""
    launch_log = tmp_path / "launches.log"
    options_log = tmp_path / "launch-options.log"
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()
    validator_server = browser_dir / "validator-server.mjs"
    validator_server.write_text(_VALIDATOR_SERVER.read_text(encoding="utf-8"), encoding="utf-8")
    _write_playwright_stub(tmp_path / "node_modules")

    env = {
        **os.environ,
        "APP_BASE_URL": "http://fixture-app.invalid",
        "AWF_VALIDATOR_HOST": "127.0.0.1",
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": "/usr/bin/chromium",
        "PLAYWRIGHT_STUB_LOG": str(launch_log),
        "PLAYWRIGHT_STUB_OPTIONS_LOG": str(options_log),
    }
    port, process = _start_validator_server(validator_server, env)

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/validate", timeout=10) as response:
            assert response.read().decode("utf-8") == (
                "browser validated awf-node-profile-fixture\n"
            )

        launch_options = json.loads(options_log.read_text(encoding="utf-8"))
        assert launch_options["executablePath"] == "/usr/bin/chromium"
        assert launch_options["headless"] is True
        assert "--no-sandbox" in launch_options["args"]
    finally:
        _terminate_process(process)
