"""Channel resolution: the requested --channel must match the resolved manifest.

When ``--version`` is omitted the installer resolves the latest release manifest,
which on GitHub is always the ``stable`` channel. The manifest itself records the
authoritative ``channel`` (see RELEASING.md: "the channel does not change artifact
URLs; version and tag pinning are the trust boundary"), so the installer enforces
the requested ``--channel`` against the resolved manifest rather than silently
ignoring it. A pinned ``--version`` selects the manifest directly, so the channel
field is then informational only.
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_channel_mismatch_aborts_before_install(harness: InstallerHarness) -> None:
    """``--channel prerelease`` against a stable manifest fails before mutation."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, channel="stable")

    result = harness.run(["--channel", "prerelease"], manifest=manifest)

    assert result.returncode != 0
    assert "CHANNEL_MISMATCH" in result.stderr
    # The mismatch must mention both channels and how to recover.
    assert "prerelease" in result.stderr
    assert "stable" in result.stderr
    assert "--version" in result.stderr
    # Abort before any install mutation.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_matching_prerelease_channel_installs(harness: InstallerHarness) -> None:
    """A prerelease manifest installs when ``--channel prerelease`` is requested."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel(version="0.2.0rc1")
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        version="0.2.0rc1",
        channel="prerelease",
    )

    result = harness.run(["--channel", "prerelease"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_default_stable_channel_matches_stable_manifest(harness: InstallerHarness) -> None:
    """The default ``stable`` channel installs a stable manifest unchanged."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, channel="stable")

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_pinned_version_skips_channel_enforcement(harness: InstallerHarness) -> None:
    """A pinned ``--version`` selects the manifest directly; channel is not enforced."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel(version="0.2.0rc1")
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        version="0.2.0rc1",
        channel="prerelease",
    )

    # Default channel is stable, but the explicit version pins the prerelease
    # manifest, so the install must proceed without a channel mismatch.
    result = harness.run(["--version", "0.2.0rc1"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "uv tool install" in "\n".join(harness.calls())
