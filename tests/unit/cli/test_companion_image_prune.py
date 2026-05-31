"""CLI helper that prunes cached companion images during ``service gc`` (#298)."""

from __future__ import annotations

import subprocess
import threading

import pytest

from awf.cli import common as cli_common


class _Result:
    def __init__(self, returncode: int, *, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.unit
async def test_prune_success_reports_reclaimed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful prune reports the space reclaimed from the docker output."""
    captured: dict[str, object] = {}

    def _run(cmd: list[str], **_kwargs: object) -> _Result:
        captured["cmd"] = cmd
        return _Result(0, stdout="Total reclaimed space: 2GB\n")

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = await cli_common._run_companion_image_prune(168)

    assert result["status"] == "succeeded"
    assert result["reason_code"] == "COMPANION_IMAGE_PRUNE_SUCCEEDED"
    assert result["output"] == "Total reclaimed space: 2GB"
    assert captured["cmd"][:3] == ["docker", "image", "prune"]
    assert "until=168h" in captured["cmd"]


@pytest.mark.unit
async def test_prune_runs_subprocess_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocking ``subprocess.run`` must be offloaded so it cannot freeze the loop."""
    event_loop_thread = threading.get_ident()
    observed: dict[str, int] = {}

    def _run(*_a: object, **_k: object) -> _Result:
        observed["thread"] = threading.get_ident()
        return _Result(0, stdout="Total reclaimed space: 0B\n")

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = await cli_common._run_companion_image_prune(24)

    assert result["status"] == "succeeded"
    assert observed["thread"] != event_loop_thread


@pytest.mark.unit
async def test_prune_nonzero_exit_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero docker exit is surfaced as a failed prune result."""
    monkeypatch.setattr(
        cli_common.subprocess,
        "run",
        lambda *_a, **_k: _Result(1, stderr="permission denied"),
    )

    result = await cli_common._run_companion_image_prune(24)

    assert result["status"] == "failed"
    assert result["reason_code"] == "COMPANION_IMAGE_PRUNE_FAILED"
    assert result["error"] == "permission denied"


@pytest.mark.unit
async def test_prune_timeout_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prune subprocess timeout is reported as a failed result."""

    def _run(*_a: object, **_k: object) -> _Result:
        raise subprocess.TimeoutExpired(cmd="docker image prune", timeout=120)

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = await cli_common._run_companion_image_prune(24)

    assert result["status"] == "failed"
    assert result["reason_code"] == "COMPANION_IMAGE_PRUNE_FAILED"
    assert "timed out" in str(result["error"])


@pytest.mark.unit
async def test_prune_os_error_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError launching the prune subprocess is reported as failed."""

    def _run(*_a: object, **_k: object) -> _Result:
        raise OSError("docker binary not found")

    monkeypatch.setattr(cli_common.subprocess, "run", _run)

    result = await cli_common._run_companion_image_prune(24)

    assert result["status"] == "failed"
    assert result["reason_code"] == "COMPANION_IMAGE_PRUNE_FAILED"
    assert "docker binary not found" in str(result["error"])
