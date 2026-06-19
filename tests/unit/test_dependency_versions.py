"""Tests that verify security-patched dependency versions in lock files.

These tests guard against regressions that would downgrade packages that were
bumped by Dependabot security alerts (see: fix(security): bump PyJWT and
@babel/core for Dependabot alerts).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent


def _parse_semver(version: str) -> tuple[int, ...]:
    """Return a comparable tuple from a dotted-version string."""
    return tuple(int(x) for x in version.split("."))


# ---------------------------------------------------------------------------
# uv.lock helpers
# ---------------------------------------------------------------------------


def _read_uv_lock() -> str:
    lock_path = REPO_ROOT / "uv.lock"
    assert lock_path.exists(), f"uv.lock not found at {lock_path}"
    return lock_path.read_text(encoding="utf-8")


def _get_uv_package_version(content: str, package_name: str) -> str:
    """Extract the resolved version for *package_name* from uv.lock content."""
    # uv.lock uses TOML-like [[package]] sections:
    #   name = "pyjwt"
    #   version = "2.13.0"
    pattern = re.compile(
        r'\[\[package\]\]\s+name\s*=\s*"'
        + re.escape(package_name)
        + r'"\s+version\s*=\s*"([^"]+)"',
        re.IGNORECASE,
    )
    match = pattern.search(content)
    assert match, f"Package '{package_name}' not found in uv.lock"
    return match.group(1)


# ---------------------------------------------------------------------------
# PyJWT security bump tests (2.12.1 -> 2.13.0)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pyjwt_version_at_least_2_13_0() -> None:
    """PyJWT must be >= 2.13.0 (Dependabot security alert fix)."""
    content = _read_uv_lock()
    version = _get_uv_package_version(content, "pyjwt")
    assert _parse_semver(version) >= _parse_semver("2.13.0"), (
        f"pyjwt version {version!r} is below the required minimum 2.13.0. "
        "This version was bumped to address a Dependabot security alert."
    )


@pytest.mark.unit
def test_pyjwt_has_valid_wheel_hash() -> None:
    """The PyJWT wheel entry in uv.lock must contain a sha256 hash."""
    content = _read_uv_lock()
    # Find pyjwt wheel line and confirm it has a hash attribute
    pattern = re.compile(
        r'name\s*=\s*"pyjwt".*?wheels\s*=\s*\[([^\]]+)\]',
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(content)
    assert match, "Could not find pyjwt wheels section in uv.lock"
    wheels_block = match.group(1)
    assert "hash = \"sha256:" in wheels_block, (
        "pyjwt wheel entry is missing a sha256 hash in uv.lock"
    )


@pytest.mark.unit
def test_pyjwt_wheel_matches_expected_hash() -> None:
    """The PyJWT 2.13.0 wheel hash must match the known-good value from PyPI."""
    expected_hash = "66adcc2aff09b3f1bbd95fc1e1577df8ac8723c978552fd43304c8a290ac5728"
    content = _read_uv_lock()
    assert expected_hash in content, (
        f"Expected pyjwt 2.13.0 wheel hash {expected_hash!r} not found in uv.lock. "
        "The lock file may reference an untrusted or incorrect wheel."
    )


@pytest.mark.unit
def test_pyjwt_sdist_matches_expected_hash() -> None:
    """The PyJWT 2.13.0 sdist hash must match the known-good value from PyPI."""
    expected_hash = "41571c89ca91598c79e8ef18a2d07367d4810fbbd6f637794879baf1b7703423"
    content = _read_uv_lock()
    assert expected_hash in content, (
        f"Expected pyjwt 2.13.0 sdist hash {expected_hash!r} not found in uv.lock. "
        "The lock file may reference an untrusted or incorrect source distribution."
    )


# ---------------------------------------------------------------------------
# Regression / boundary helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_semver_ordering() -> None:
    """_parse_semver must produce tuples that compare correctly."""
    assert _parse_semver("2.13.0") > _parse_semver("2.12.1")
    assert _parse_semver("2.12.1") < _parse_semver("2.13.0")
    assert _parse_semver("2.13.0") == _parse_semver("2.13.0")
    assert _parse_semver("3.0.0") > _parse_semver("2.99.99")


@pytest.mark.unit
def test_uv_lock_file_is_readable() -> None:
    """uv.lock must exist and be non-empty."""
    content = _read_uv_lock()
    assert len(content) > 0, "uv.lock is empty"
    assert "[[package]]" in content, "uv.lock does not appear to be a valid uv lock file"
