"""Unit tests for the build- and install-failure classification in
``tests.packaging_build``.

These pin the behavior two PR #344 review threads asked for:

* ``PRRT_kwDOSJAM6s6F-3j5`` — a ``uv build`` that cannot set up its environment
  (no ``uv``, offline, backend unfetchable) is *unavailable* and skips, but a
  build that runs against this checkout and fails to produce artifacts is a real
  packaging regression that fails loudly instead of skipping.
* ``PRRT_kwDOSJAM6s6F-8ys`` — a ``pip install <wheel>`` that fails because
  dependencies cannot be fetched offline is environmental and skips, but a
  failure with the local wheel artifact itself (invalid metadata, incompatible
  Requires-Python, malformed archive) is a real regression that fails loudly.
* ``PRRT_kwDOSJAM6s6F_EeW`` — a *pure* pip resolver miss ("could not find a
  version that satisfies the requirement" / "no matching distribution found")
  with no transport marker means the index was reachable but the wheel declares
  an impossible/misspelled runtime dependency, so it is a wheel-metadata
  regression that fails loudly; genuine offline still skips because pip emits a
  transport marker while retrying the unreachable index.

They stub ``subprocess.run``/``shutil.which`` so no actual build (or network) is
exercised.
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
    install_failure_is_environmental,
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


@pytest.mark.parametrize(
    "output",
    [
        # Genuine offline: pip cannot reach an index, so it surfaces a transport /
        # DNS / connection failure while retrying. These transport markers — not
        # the terminal resolver-miss text — are what identifies an offline lane.
        "WARNING: Retrying ... after connection broken by 'NewConnectionError(...)'",
        "Temporary failure in name resolution",
        "Failed to establish a new connection: [Errno -3]",
        "Could not connect to pypi.org",
        "Read timed out.",
        "Max retries exceeded with url: /simple/anyio/",
        # A realistic offline install emits BOTH a transport marker (the network
        # attempt that failed) AND a terminal resolver miss; the transport marker
        # keeps it classified as environmental even though the resolver-miss text
        # is no longer a signature on its own (PRRT_kwDOSJAM6s6F_EeW).
        (
            "WARNING: Retrying ... after connection broken by 'NewConnectionError(...)'\n"
            "ERROR: Could not find a version that satisfies the requirement structlog "
            "(from versions: none)\n"
            "ERROR: No matching distribution found for structlog"
        ),
    ],
)
def test_install_offline_failures_are_classified_environmental(output: str) -> None:
    assert install_failure_is_environmental(output) is True


@pytest.mark.parametrize(
    "output",
    [
        # Real wheel-artifact regressions the package guard must catch even when
        # venv + pip are available (PRRT_kwDOSJAM6s6F-8ys). None contain a
        # network/offline marker, so they must classify as non-environmental.
        "ERROR: awf-0.1.0-py3-none-any.whl is not a supported wheel on this platform.",
        "ERROR: Package 'awf' requires a different Python: 3.11.9 not in '>=3.12'",
        "ERROR: Invalid wheel filename (wrong number of parts): awf-bad.whl",
        "error: invalid metadata: Metadata-Version is not declared",
        "zipfile.BadZipFile: File is not a zip file",
        # A pure resolver miss with NO transport marker: pip reached the index and
        # reported the wheel's declared dependency as unsatisfiable. On a networked
        # lane this is an impossible / misspelled runtime dependency — a real
        # wheel-metadata regression that must fail loudly, not skip as "offline"
        # (PRRT_kwDOSJAM6s6F_EeW).
        "ERROR: Could not find a version that satisfies the requirement requets (from versions: none)",
        "ERROR: No matching distribution found for nonexistent-typo-dependency",
    ],
)
def test_install_real_regressions_are_not_classified_environmental(output: str) -> None:
    assert install_failure_is_environmental(output) is False


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
