"""Unsupported OS/arch fails before any download or install mutation."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("MINGW64_NT-10.0", "x86_64"),
        ("Windows_NT", "x86_64"),
        ("Linux", "ppc64le"),
        ("Linux", "i686"),
        ("Darwin", "armv7l"),
    ],
)
def test_unsupported_platform_fails_before_mutation(
    harness: InstallerHarness,
    system: str,
    machine: str,
) -> None:
    """An OS or arch outside the allowlist aborts before download/install."""
    harness.add_uname(system, machine)
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "UNSUPPORTED_PLATFORM" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("system", "machine"),
    [("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "x86_64"), ("Linux", "aarch64")],
)
def test_supported_platforms_pass_the_guard(
    harness: InstallerHarness,
    system: str,
    machine: str,
) -> None:
    """Supported macOS/Linux arches survive the platform guard into install."""
    harness.add_uname(system, machine)
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "UNSUPPORTED_PLATFORM" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())
