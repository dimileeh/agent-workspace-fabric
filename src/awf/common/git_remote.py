"""Best-effort origin-remote detection for preview/smoke forge resolution.

``awf profile preview`` and the smoke disk-preview path resolve a profile from a
local checkout but have no request-supplied ``repo_url``, so ``forge: auto``
would always fall through to the default ``github`` — even for a checkout whose
``origin`` is a ``bitbucket.org`` remote. Reading the checkout's ``origin`` URL
here lets those tooling paths feed the same forge detection the real provision
path uses, so the preview reflects the checkout instead of masking it.

Detection is best-effort: any failure (not a git repo, no ``origin`` remote, git
missing, or a slow call) yields ``None`` and the resolver falls through to its
default forge exactly as before. This never gates execution — the executor forge
gate remains the fail-fast point.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ("detect_repo_url_from_checkout",)


def detect_repo_url_from_checkout(path: Path) -> str | None:
    """Return the ``origin`` remote URL for a local checkout, or ``None``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
