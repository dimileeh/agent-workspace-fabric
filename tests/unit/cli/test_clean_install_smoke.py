"""Clean-install ``--help`` smoke from outside the source checkout (T13, AC1).

The primary test extracts the built wheel and runs the first-run ``--help``
commands in a subprocess whose import path puts the extracted wheel first and
drops the editable repo ``src`` — proving the packaged ``awf`` runs from outside
the checkout without repo-relative files. It works offline (runtime deps resolve
from the current interpreter). A second, best-effort test performs a real
``uv venv`` + ``uv pip install`` of the wheel: it skips only when its
dependencies cannot be fetched offline, but fails on a real wheel-artifact
regression (invalid metadata, incompatible Requires-Python, malformed archive);
CI (with network) runs it fully. The exhaustive install-lane matrix is owned by
T14.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.packaging_build import (
    REPO_ROOT,
    BuiltDistributions,
    PackageBuildUnavailableError,
    build_distributions,
    extract_wheel,
    install_failure_is_environmental,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

_HELP_COMMANDS = (
    ["--help"],
    ["setup", "--help"],
    ["start", "--help"],
    ["init", "--help"],
    ["mcp", "serve", "--help"],
)
_SUCCESS_MARKER = "AWF_HELP_SMOKE_OK"

# Driver run in a clean subprocess: make the extracted wheel win over the
# editable repo ``src`` (and any shadow ``awf`` on site-packages), then assert
# provenance before exercising the first-run ``--help`` surfaces.
_DRIVER = f"""
import os
import sys

extract = os.path.abspath(os.environ["AWF_WHEEL_EXTRACT"])
repo_src = os.path.abspath(os.environ["AWF_REPO_SRC"])
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != repo_src]
sys.path.insert(0, extract)

import awf

if not os.path.abspath(awf.__file__).startswith(extract):
    sys.stderr.write("awf resolved outside the wheel: " + awf.__file__ + "\\n")
    raise SystemExit(2)

from typer.testing import CliRunner

from awf.cli.main import app

runner = CliRunner()
for args in {list(_HELP_COMMANDS)!r}:
    result = runner.invoke(app, args)
    if result.exit_code != 0:
        sys.stderr.write("help failed for " + " ".join(args) + ":\\n" + result.output + "\\n")
        raise SystemExit(3)

print({_SUCCESS_MARKER!r})
"""


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stderr, result.stdout) if part)


def _output_tail(result: subprocess.CompletedProcess[str], *, length: int = 600) -> str:
    combined = _combined_output(result).strip()
    return combined[-length:] if combined else "<no subprocess output>"


@pytest.fixture(scope="module")
def built() -> BuiltDistributions:
    """Build the wheel once for this module, skipping if unavailable."""
    try:
        return build_distributions()
    except PackageBuildUnavailableError as exc:  # pragma: no cover - env dependent
        pytest.skip(str(exc))


def test_extracted_wheel_help_runs_outside_checkout(
    built: BuiltDistributions,
    tmp_path: Path,
) -> None:
    """The packaged ``awf`` runs every first-run ``--help`` from outside the checkout."""
    extract_dir = extract_wheel(built.wheel, tmp_path / "wheel")
    outside = tmp_path / "outside"
    outside.mkdir()
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(driver)],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(outside),
        env={
            # Inherit the parent environment (keeps SystemRoot for Python on
            # Windows and the real system PATH) so the override stays
            # cross-platform; the driver enforces wheel provenance via sys.path.
            **os.environ,
            "AWF_WHEEL_EXTRACT": str(extract_dir),
            "AWF_REPO_SRC": str((REPO_ROOT / "src").resolve()),
        },
        timeout=300,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert _SUCCESS_MARKER in result.stdout


def test_uv_venv_install_help(built: BuiltDistributions, tmp_path: Path) -> None:
    """A real uv-managed venv install exposes a working ``awf --help`` (CI lane).

    Skips when ``uv``/Python setup is unavailable or when the wheel's
    dependencies cannot be fetched offline, keeping the unit suite robust; but a
    non-environmental install failure (a regression in the wheel artifact
    itself) fails the test so the package guard is preserved. CI with network
    runs the full install.
    """
    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover - env dependent
        pytest.skip("uv is not available for the clean wheel install smoke")

    venv_dir = tmp_path / "venv"
    try:
        create = subprocess.run(
            [uv, "venv", "--python", "3.12", str(venv_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"uv venv could not run: {exc}")
    if create.returncode != 0:  # pragma: no cover - env dependent
        pytest.skip(f"uv venv unavailable: {_output_tail(create)}")

    venv_python = venv_dir / "bin" / "python"
    venv_awf = venv_dir / "bin" / "awf"
    if not venv_python.exists():  # pragma: no cover - non-POSIX layout
        pytest.skip("venv python interpreter not found at the expected POSIX path")

    try:
        install = subprocess.run(
            [uv, "pip", "install", "--python", str(venv_python), str(built.wheel)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"uv wheel install could not run: {exc}")
    if install.returncode != 0:  # pragma: no cover - depends on offline vs regression outcome
        combined = _combined_output(install)
        if install_failure_is_environmental(combined):
            pytest.skip(
                "uv wheel install with dependencies unavailable offline: " + _output_tail(install)
            )
        # uv processed the local wheel but the install failed for a
        # non-environmental reason — invalid wheel metadata, an incompatible
        # Requires-Python, or a malformed archive. Fail loudly so the
        # package-artifact guard catches the regression instead of reporting
        # skipped (PRRT_kwDOSJAM6s6F-8ys).
        pytest.fail(f"uv wheel install failed for a non-environmental reason:\n{combined[-1000:]}")

    if not venv_awf.exists():  # pragma: no cover - wheel entry-point regression
        # uv install succeeded but the ``awf`` console script is absent (e.g. a
        # malformed ``entry_points.txt`` in the wheel). Fail with a diagnostic
        # message rather than letting the subprocess call below raise a bare
        # FileNotFoundError — a missing entry point on this POSIX layout is a
        # real wheel-artifact regression the package guard must surface.
        pytest.fail(
            f"uv install succeeded but the 'awf' console script is missing at {venv_awf}; "
            "the wheel's entry-point metadata is malformed."
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    for args in _HELP_COMMANDS:
        result = subprocess.run(
            [str(venv_awf), *args],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(outside),
            timeout=120,
        )
        assert result.returncode == 0, f"{args}: {result.stderr}"

    provenance = subprocess.run(
        [str(venv_python), "-c", "import awf; print(awf.__file__)"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(outside),
        timeout=60,
    )
    assert provenance.returncode == 0, f"stdout={provenance.stdout}\nstderr={provenance.stderr}"
    assert str(venv_dir) in provenance.stdout, provenance.stdout
