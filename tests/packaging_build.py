"""Shared build-backed packaging helpers for the T13 package-content tests.

The dev ``[extra]`` does not install ``hatchling`` (it is only the PEP 517 build
backend), so the in-process ``hatchling.build`` hooks are unavailable. These
helpers instead drive the repo's standard build frontend, ``uv build``, into a
process-lifetime temp directory and hand tests the built wheel/sdist paths.

Building is gated behind ``uv`` availability and a populated build environment:
when ``uv`` is missing, or the build environment cannot be set up offline (the
build backend cannot be fetched), callers ``pytest.skip`` via
:class:`PackageBuildUnavailableError` so the unit suite stays robust on hosts
without a populated build environment. A build that actually runs against this
checkout but fails to produce artifacts — a real packaging regression such as a
malformed ``pyproject.toml``, missing build input, or broken artifact
generation — raises :class:`PackageBuildFailedError`, which is *not* on the skip
path, so the package-artifact guard fails loudly instead of reporting skipped.
CI, which has network and builds release artifacts, exercises the full path.

The build runs at most once per worker process (``functools.lru_cache``); under
``pytest -n … --dist=loadscope`` each module-scoped group lands on one worker, so
the amortized cost is one build per worker that needs an artifact.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# tests/packaging_build.py -> repo root is one level up from tests/.
REPO_ROOT = Path(__file__).resolve().parents[1]
_BUILD_TIMEOUT_SECONDS = 600


class PackageBuildUnavailableError(RuntimeError):
    """Raised when the wheel/sdist cannot be built because the build environment
    is not set up: ``uv`` is missing, the build could not start, or it failed to
    fetch the build backend offline. Build-backed tests ``pytest.skip`` on this
    so the unit suite stays robust on hosts without a populated build environment.
    """


class PackageBuildFailedError(AssertionError):
    """Raised when ``uv build`` ran against this checkout but failed to produce
    artifacts — a real packaging regression (malformed ``pyproject.toml``,
    missing build input, broken artifact generation). Subclassing
    :class:`AssertionError` keeps this off the unavailable/skip path so the
    package-artifact guard fails loudly rather than reporting skipped.
    """


# Substrings in a nonzero ``uv build`` output that mark the failure as an
# unpopulated build environment (offline / the build backend cannot be fetched)
# rather than a real packaging regression. A real offline build still surfaces
# one of these because ``uv`` reports the network/cache miss; anything else is
# treated as a genuine build failure. Kept network-focused on purpose so a real
# regression is never masked as "unavailable".
_BUILD_ENV_UNAVAILABLE_SIGNATURES = (
    "network connectivity is disabled",
    # Match the specific "offline mode" phrase uv emits for ``--offline`` rather
    # than a bare "offline" substring: the bare substring would also mask a real
    # packaging regression whose error text merely mentions the word "offline"
    # (e.g. an extra/dependency named that), skipping instead of failing the
    # artifact guard. uv's genuine offline cache miss is already covered above by
    # "network connectivity is disabled".
    "offline mode",
    "failed to fetch",
    "failed to download",
    "error sending request",
    "could not connect",
    "could not resolve host",
    "failed to lookup address",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset",
    "no such host",
    "operation timed out",
)


def _build_failure_is_environmental(output: str) -> bool:
    """Return ``True`` when a nonzero ``uv build`` looks like an unpopulated build
    environment (offline / build backend unfetchable) rather than a regression."""
    lowered = output.lower()
    return any(signature in lowered for signature in _BUILD_ENV_UNAVAILABLE_SIGNATURES)


# Substrings in a nonzero wheel-install output that mark the failure as an
# offline dependency-fetch problem (uv/pip cannot reach an index to resolve the
# wheel's runtime dependencies) rather than a real regression in the local wheel
# artifact itself. The installer handles the explicitly-passed local archive
# directly, so a bad wheel (invalid metadata, incompatible Requires-Python,
# malformed archive) fails with artifact-specific text that contains none of
# these.
#
# Kept strictly transport/DNS/connection-focused on purpose. pip's terminal
# resolver-miss messages ("could not find a version that satisfies the
# requirement" / "no matching distribution found") are deliberately *excluded*:
# they also appear when the wheel declares an impossible or misspelled runtime
# dependency, which on a networked lane is a real wheel-metadata regression, not
# an offline setup problem. A genuine offline run still skips because pip emits
# one of the transport markers below while retrying the unreachable index before
# it gives up with a resolver miss — so dropping the resolver-miss substrings
# never masks an offline lane, but it does let a *pure* resolver miss (index
# reachable, declared dependency unsatisfiable) fail the package guard loudly
# instead of skipping (PRRT_kwDOSJAM6s6F-8ys, PRRT_kwDOSJAM6s6F_EeW).
_INSTALL_ENV_UNAVAILABLE_SIGNATURES = (
    # Transport / DNS / connection failures surfaced by pip's vendored urllib3.
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve host",
    "no such host",
    "network is unreachable",
    "failed to establish a new connection",
    "newconnectionerror",
    "max retries exceeded",
    "connection refused",
    "connection reset",
    "connection timed out",
    "read timed out",
    "operation timed out",
    "could not connect",
    "proxyerror",
    "error sending request",
    "failed to fetch",
    "failed to download",
)


def install_failure_is_environmental(output: str) -> bool:
    """Return ``True`` when a nonzero wheel install looks like an offline
    dependency-fetch problem rather than a regression in the wheel artifact
    itself.

    The clean-install smoke test (``test_uv_venv_install_help``) skips on
    environmental failures so the unit suite stays robust without network, but
    fails loudly on anything else — invalid wheel metadata, an incompatible
    Requires-Python, or a malformed archive — to preserve the package-artifact
    guard (PRRT_kwDOSJAM6s6F-8ys)."""
    lowered = output.lower()
    return any(signature in lowered for signature in _INSTALL_ENV_UNAVAILABLE_SIGNATURES)


@dataclass(frozen=True)
class BuiltDistributions:
    """Paths to the built wheel and sdist for the current repo checkout."""

    wheel: Path
    sdist: Path


@lru_cache(maxsize=1)
def build_distributions() -> BuiltDistributions:
    """Build the AWF wheel and sdist once per process via ``uv build``.

    Raises :class:`PackageBuildUnavailableError` when the build environment is not
    set up (``uv`` missing, build cannot start, or the backend cannot be fetched
    offline) so build-backed tests can skip cleanly. Raises
    :class:`PackageBuildFailedError` when ``uv build`` runs against this checkout
    but fails to produce artifacts, so a real packaging regression fails the
    tests instead of skipping.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise PackageBuildUnavailableError("uv is not available to build distributions")

    out_dir = Path(tempfile.mkdtemp(prefix="awf-pkg-build-"))
    # The built artifacts must outlive this call (tests reference the wheel/sdist
    # paths for the rest of the process), so defer cleanup to process exit rather
    # than a context manager. This also reclaims temp dirs from failed build
    # attempts, which lru_cache does not cache.
    atexit.register(shutil.rmtree, out_dir, ignore_errors=True)
    try:
        result = subprocess.run(
            [uv, "build", "--out-dir", str(out_dir), str(REPO_ROOT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_BUILD_TIMEOUT_SECONDS,
            cwd=str(REPO_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackageBuildUnavailableError(f"uv build could not run: {exc}") from exc

    if result.returncode != 0:
        output = "\n".join(part for part in (result.stderr, result.stdout) if part)
        tail = " ".join(output.strip().splitlines()[-5:])
        if _build_failure_is_environmental(output):
            raise PackageBuildUnavailableError(
                "uv build could not set up a build environment "
                "(offline / build backend unavailable): " + tail
            )
        # The build ran against this checkout and failed for a non-environmental
        # reason — a real packaging regression. Fail loudly instead of skipping.
        raise PackageBuildFailedError(
            "uv build ran against this checkout but failed to produce artifacts "
            "(packaging regression): " + tail
        )

    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))
    if not wheels or not sdists:
        # uv build reported success yet emitted nothing usable — broken artifact
        # generation is a regression, not an unavailable environment.
        raise PackageBuildFailedError(
            "uv build reported success but did not produce both a wheel and an sdist"
        )
    return BuiltDistributions(wheel=wheels[0], sdist=sdists[0])


def pyproject_project_version() -> str:
    """Return the ``[project].version`` declared in the repo ``pyproject.toml``."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["version"])


def extract_wheel(wheel: Path, destination: Path) -> Path:
    """Extract ``wheel`` into ``destination`` and return the extraction root."""
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(destination)
    return destination
