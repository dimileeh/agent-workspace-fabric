"""Offline Antigravity CLI smoke — no live API key required."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PINNED_VERSION = "1.1.13"
_PINNED_PATH = Path("/usr/local/bin/agy")


def _resolve_agy() -> Path | None:
    if _PINNED_PATH.is_file() and os.access(_PINNED_PATH, os.X_OK):
        return _PINNED_PATH
    which = shutil.which("agy")
    return Path(which) if which else None


@pytest.mark.docker
def test_agy_offline_binary_version_and_flags() -> None:
    """Offline contract: binary present, version matches pin, headless flags accepted."""
    agy = _resolve_agy()
    if agy is None:
        pytest.skip("agy not installed in this environment (agent-runtime image rebuild pending)")

    version = subprocess.run(
        [str(agy), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert _PINNED_VERSION in version.stdout or _PINNED_VERSION in version.stderr

    help_proc = subprocess.run(
        [str(agy), "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    help_text = f"{help_proc.stdout}\n{help_proc.stderr}"
    assert "--dangerously-skip-permissions" in help_text
    assert "--output-format" in help_text
    assert "--print" in help_text or "-p" in help_text
    assert "--model" in help_text

    # Bad flag remains distinct (exit 2) — offline auth-free assertion.
    bad = subprocess.run(
        [str(agy), "--not-a-real-flag"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert bad.returncode == 2
