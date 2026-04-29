"""Regression tests for the Node browser validation fixture."""

from __future__ import annotations

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
              async launch() {
                appendFileSync(process.env.PLAYWRIGHT_STUB_LOG, "launch\\n", "utf8");
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


class _HangingHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(30)

    def log_message(self, format: str, *args: object) -> None:
        return


class _HangingHealthServer(ThreadingHTTPServer):
    daemon_threads = True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_healthcheck_fetch_times_out_when_service_hangs() -> None:
    """A hung health endpoint should fail inside the script-level fetch timeout."""
    server = _HangingHealthServer(("127.0.0.1", 0), _HangingHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        port = server.server_address[1]
        result = subprocess.run(
            ["node", "scripts/healthcheck.mjs", "app"],
            cwd=_FIXTURE_ROOT,
            env={
                **os.environ,
                "APP_BASE_URL": f"http://127.0.0.1:{port}",
                "AWF_HEALTHCHECK_FETCH_TIMEOUT_MS": "50",
            },
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode != 0
    assert "timed out fetching app health response after 50ms" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_validator_server_reuses_browser_for_concurrent_validation_requests(
    tmp_path: Path,
) -> None:
    """Concurrent validations should share one browser process."""
    port = _free_port()
    launch_log = tmp_path / "launches.log"
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()
    validator_server = browser_dir / "validator-server.mjs"
    validator_server.write_text(_VALIDATOR_SERVER.read_text(encoding="utf-8"), encoding="utf-8")
    _write_playwright_stub(tmp_path / "node_modules")

    env = {
        **os.environ,
        "APP_BASE_URL": "http://fixture-app.invalid",
        "PLAYWRIGHT_STUB_LOG": str(launch_log),
        "PORT": str(port),
    }
    process = subprocess.Popen(
        ["node", str(validator_server)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_healthz(port, process)
        validate_url = f"http://127.0.0.1:{port}/validate"

        def validate() -> str:
            with urllib.request.urlopen(validate_url, timeout=10) as response:
                return response.read().decode("utf-8")

        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(lambda _: validate(), range(6)))

        assert responses == ["browser validated awf-node-profile-fixture\n"] * 6
        assert launch_log.read_text(encoding="utf-8").splitlines() == ["launch"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
