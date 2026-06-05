"""Focused integration coverage for first-run source checkout smoke lanes."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import first_run_smoke as smoke

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.timeout(900)]

_SOURCE_COMMAND_TIMEOUT_SECONDS = 120


def test_source_uv_run_lane_proves_checkout_from_outside(tmp_path: Path) -> None:
    """The no-global source lane runs setup/start/help from outside the checkout."""
    if shutil.which("uv") is None:  # pragma: no cover - environment dependent
        pytest.skip("uv is not available")

    results = smoke.run_source_uv_run_lane(
        checkout_root=smoke.REPO_ROOT,
        smoke_root=tmp_path,
        timeout_seconds=_SOURCE_COMMAND_TIMEOUT_SECONDS,
    )

    _assert_no_environmental_skip(results)
    assert all(result.status == "passed" for result in results), results
    source_checkout_results = [result for result in results if result.source_checkout is not None]
    assert len(source_checkout_results) == 1, results
    source_checkout = source_checkout_results[0].source_checkout
    assert isinstance(source_checkout, dict)
    assert source_checkout.get("root") == str((tmp_path / "source-checkout").resolve())


def test_source_tool_install_lane_installs_isolated_awf(tmp_path: Path) -> None:
    """The global source lane installs with uv tool into isolated temp dirs."""
    if shutil.which("uv") is None:  # pragma: no cover - environment dependent
        pytest.skip("uv is not available")

    results = smoke.run_source_tool_install_lane(
        checkout_root=smoke.REPO_ROOT,
        smoke_root=tmp_path,
        timeout_seconds=_SOURCE_COMMAND_TIMEOUT_SECONDS,
    )

    _assert_no_environmental_skip(results)
    assert all(result.status == "passed" for result in results), results
    install_result = results[0]
    assert "uv tool install" in " ".join(install_result.command)
    source_checkout_results = [result for result in results if result.source_checkout is not None]
    assert len(source_checkout_results) == 1, results
    source_checkout = source_checkout_results[0].source_checkout
    assert isinstance(source_checkout, dict)
    assert source_checkout.get("root") == str((tmp_path / "source-checkout").resolve())


def test_environmental_skip_helper_fails_when_later_result_failed() -> None:
    """An early environmental skip must not mask the source-checkout proof failure."""
    results = (
        smoke.SmokeResult(
            lane=smoke.Lane.SOURCE_UV_RUN,
            status="skipped",
            command=("uv", "run", "awf", "--help"),
            reason="smoke command could not resolve dependencies in this environment",
        ),
        smoke.SmokeResult(
            lane=smoke.Lane.SOURCE_UV_RUN,
            status="failed",
            command=("uv", "run", "awf", "setup", "--dry-run"),
            reason="source checkout failed validation: SOURCE_CHECKOUT_INVALID",
        ),
    )

    # Catch skip too so this regression reports a test failure, not a skipped test.
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as exc_info:
        _assert_no_environmental_skip(results)

    assert isinstance(exc_info.value, pytest.fail.Exception)
    assert "SOURCE_CHECKOUT_INVALID" in str(exc_info.value)


def _assert_no_environmental_skip(results: tuple[smoke.SmokeResult, ...]) -> None:
    failed = [result for result in results if result.status == "failed"]
    if failed:
        pytest.fail("; ".join(result.reason for result in failed))
    skipped = [result for result in results if result.status == "skipped"]
    if skipped:  # pragma: no cover - environment dependent
        pytest.skip("; ".join(result.reason for result in skipped))
