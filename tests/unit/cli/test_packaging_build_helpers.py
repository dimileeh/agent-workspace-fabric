"""Unit tests for the build-failure classification in ``tests.packaging_build``.

These pin the behavior the PR #344 review thread (PRRT_kwDOSJAM6s6F-3j5) asked
for: a ``uv build`` that cannot set up its environment (no ``uv``, offline,
backend unfetchable) is *unavailable* and skips, but a build that runs against
this checkout and fails to produce artifacts is a real packaging regression that
fails loudly instead of skipping. They stub ``subprocess.run``/``shutil.which``
so no actual build (or network) is exercised.
"""

from __future__ import annotations

import subprocess

import pytest

from tests import packaging_build
from tests.packaging_build import (
    PackageBuildFailedError,
    PackageBuildUnavailableError,
    _build_failure_is_environmental,
    build_distributions,
)

pytestmark = pytest.mark.unit


def _completed(
    returncode: int, *, stderr: str = "", stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["uv", "build"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(autouse=True)
def _clear_build_cache() -> None:
    """``build_distributions`` is ``lru_cache``d — reset it around each case."""
    build_distributions.cache_clear()
    yield
    build_distributions.cache_clear()


@pytest.mark.parametrize(
    "output",
    [
        "Network connectivity is disabled, but the requested data wasn't found in the cache",
        "error sending request for url (https://pypi.org/simple/hatchling/)",
        "failed to fetch: could not connect to pypi.org",
        "Temporary failure in name resolution",
        "Using --offline mode",
    ],
)
def test_environmental_failures_are_classified_unavailable(output: str) -> None:
    assert _build_failure_is_environmental(output) is True


@pytest.mark.parametrize(
    "output",
    [
        "error: Failed to parse `pyproject.toml`: missing field `name`",
        "ValueError: hatchling: no files found for the wheel target",
        "Traceback (most recent call last): KeyError: 'version'",
        # Regression guard (PRRT_kwDOSJAM6s6F-8ou): a real packaging failure whose
        # text merely contains the word "offline" must NOT be treated as an
        # unavailable environment, otherwise the artifact guard skips instead of
        # failing. A bare "offline" substring previously masked exactly this.
        "error: Failed to parse `pyproject.toml`: unknown extra `offline`",
    ],
)
def test_real_regressions_are_not_classified_unavailable(output: str) -> None:
    assert _build_failure_is_environmental(output) is False


def test_missing_uv_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaging_build.shutil, "which", lambda _name: None)
    with pytest.raises(PackageBuildUnavailableError, match="uv is not available"):
        build_distributions()


def test_build_cannot_start_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaging_build.shutil, "which", lambda _name: "/usr/bin/uv")

    def _raise(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("uv binary is not executable")

    monkeypatch.setattr(packaging_build.subprocess, "run", _raise)
    with pytest.raises(PackageBuildUnavailableError, match="could not run"):
        build_distributions()


def test_offline_build_failure_skips_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaging_build.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        packaging_build.subprocess,
        "run",
        lambda *_a, **_k: _completed(1, stderr="error sending request for url ... failed to fetch"),
    )
    with pytest.raises(PackageBuildUnavailableError, match="offline / build backend unavailable"):
        build_distributions()


def test_real_build_regression_fails_instead_of_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaging_build.shutil, "which", lambda _name: "/usr/bin/uv")
    monkeypatch.setattr(
        packaging_build.subprocess,
        "run",
        lambda *_a, **_k: _completed(2, stderr="error: Failed to parse `pyproject.toml`"),
    )
    with pytest.raises(PackageBuildFailedError, match="packaging regression"):
        build_distributions()
    # A real regression must never masquerade as an unavailable build.
    assert not issubclass(PackageBuildFailedError, PackageBuildUnavailableError)


def test_successful_build_without_artifacts_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packaging_build.shutil, "which", lambda _name: "/usr/bin/uv")
    # returncode 0 but the out-dir stays empty (no *.whl / *.tar.gz globbed).
    monkeypatch.setattr(packaging_build.subprocess, "run", lambda *_a, **_k: _completed(0))
    with pytest.raises(PackageBuildFailedError, match="did not produce both a wheel and an sdist"):
        build_distributions()
