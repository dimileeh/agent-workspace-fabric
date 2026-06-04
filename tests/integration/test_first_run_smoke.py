"""Focused integration coverage for first-run source checkout smoke lanes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import first_run_smoke as smoke

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.timeout(900)]


def test_source_uv_run_lane_proves_checkout_from_outside(tmp_path: Path) -> None:
    """The no-global source lane runs setup/start/help from outside the checkout."""
    if shutil.which("uv") is None:  # pragma: no cover - environment dependent
        pytest.skip("uv is not available")

    results = smoke.run_source_uv_run_lane(
        checkout_root=smoke.REPO_ROOT,
        smoke_root=tmp_path,
        timeout_seconds=600,
    )

    _assert_no_environmental_skip(results)
    assert all(result.status == "passed" for result in results), results
    setup_results = [result for result in results if "setup" in result.command]
    assert setup_results
    payload = json.loads(setup_results[-1].stdout_tail)
    assert isinstance(payload, dict)
    details = payload.get("details")
    assert isinstance(details, dict)
    source_checkout = details.get("source_checkout")
    assert isinstance(source_checkout, dict)
    assert source_checkout.get("root") == str((tmp_path / "source-checkout").resolve())
    assert "SOURCE_CHECKOUT_INVALID" not in setup_results[-1].stdout_tail


def test_source_tool_install_lane_installs_isolated_awf(tmp_path: Path) -> None:
    """The global source lane installs with uv tool into isolated temp dirs."""
    if shutil.which("uv") is None:  # pragma: no cover - environment dependent
        pytest.skip("uv is not available")

    results = smoke.run_source_tool_install_lane(
        checkout_root=smoke.REPO_ROOT,
        smoke_root=tmp_path,
        timeout_seconds=600,
    )

    _assert_no_environmental_skip(results)
    assert all(result.status == "passed" for result in results), results
    install_result = results[0]
    assert "uv tool install" in " ".join(install_result.command)
    setup_results = [result for result in results if "setup" in result.command]
    assert setup_results
    assert "SOURCE_CHECKOUT_INVALID" not in setup_results[-1].stdout_tail


def _assert_no_environmental_skip(results: tuple[smoke.SmokeResult, ...]) -> None:
    skipped = [result for result in results if result.status == "skipped"]
    if skipped:  # pragma: no cover - environment dependent
        pytest.skip("; ".join(result.reason for result in skipped))
