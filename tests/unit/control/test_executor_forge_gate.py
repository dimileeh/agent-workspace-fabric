"""Executor forge-support gate (issue #345).

Part 1 made a BitBucket workspace fail fast at this gate with
``FORGE_NOT_SUPPORTED``. Part 2 flips the gate: BitBucket Cloud is now a supported
forge, so ``unsupported_forge_error`` returns ``None`` (the workspace proceeds) for
github and bitbucket alike, and only fires for a genuinely-unknown forge.

These are focused unit tests of the pure gate decision (``unsupported_forge_error``).
The earlier end-to-end ``execute()`` fail-fast scenarios are gone because the gate no
longer trips for bitbucket; full BitBucket monitor execution is gated by the
forge-neutral-error integration work flagged in the issue #345 Part 2 PR (the
``pr_monitor_runner`` still catches ``GitHubClientError`` specifically).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.control.executor.forge_gate import unsupported_forge_error


def _ws(resolved_profile: object, repo_url: str) -> SimpleNamespace:
    return SimpleNamespace(resolved_profile=resolved_profile, repo_url=repo_url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "resolved_profile, repo_url",
    [
        ({"forge": "bitbucket"}, "git@bitbucket.org:workspace/repo.git"),
        # Missing snapshot + bitbucket repo_url → detected, now supported → proceeds.
        (None, "git@bitbucket.org:workspace/repo.git"),
        # Legacy snapshot (no forge key reconstructs as auto) + bitbucket repo_url.
        ({}, "https://bitbucket.org/workspace/repo"),
        ({"forge": "github"}, "https://github.com/owner/repo.git"),
        (None, "https://github.com/owner/repo.git"),
    ],
)
def test_supported_forge_proceeds(resolved_profile: object, repo_url: str) -> None:
    assert unsupported_forge_error(_ws(resolved_profile, repo_url)) is None


@pytest.mark.unit
def test_unknown_forge_still_fails_fast() -> None:
    # A persisted concrete forge AWF does not implement still trips the gate.
    error = unsupported_forge_error(_ws({"forge": "gitlab"}, "https://gitlab.com/o/r.git"))
    assert error is not None
    assert error.reason_code == "FORGE_NOT_SUPPORTED"
