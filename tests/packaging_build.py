"""Shared build-backed packaging helpers for the T13 package-content tests.

The dev ``[extra]`` does not install ``hatchling`` (it is only the PEP 517 build
backend), so the in-process ``hatchling.build`` hooks are unavailable. These
helpers instead drive the repo's standard build frontend, ``uv build``, into a
process-lifetime temp directory and hand tests the built wheel/sdist paths.

Building is gated behind ``uv`` availability and a successful (possibly offline)
build; callers ``pytest.skip`` via :class:`PackageBuildUnavailableError` when the
build cannot run so the unit suite stays robust on hosts without a populated
build environment. CI, which builds release artifacts, exercises the full path.

The build runs at most once per worker process (``functools.lru_cache``); under
``pytest -n … --dist=loadscope`` each module-scoped group lands on one worker, so
the amortized cost is one build per worker that needs an artifact.
"""

from __future__ import annotations

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
    """Raised when the wheel/sdist cannot be built in this environment."""


@dataclass(frozen=True)
class BuiltDistributions:
    """Paths to the built wheel and sdist for the current repo checkout."""

    wheel: Path
    sdist: Path


@lru_cache(maxsize=1)
def build_distributions() -> BuiltDistributions:
    """Build the AWF wheel and sdist once per process via ``uv build``.

    Raises :class:`PackageBuildUnavailableError` (never a hard error) when ``uv`` is
    missing or the build fails, so build-backed tests can skip cleanly.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise PackageBuildUnavailableError("uv is not available to build distributions")

    out_dir = Path(tempfile.mkdtemp(prefix="awf-pkg-build-"))
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
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise PackageBuildUnavailableError(
            "uv build failed (likely no network/build backend): " + " ".join(tail)
        )

    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))
    if not wheels or not sdists:
        raise PackageBuildUnavailableError("uv build did not produce both a wheel and an sdist")
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
