"""A pinned --version must match the resolved manifest's own version/source.tag.

``--version`` is the documented trust boundary (RELEASING.md: "version and tag
pinning are the trust boundary"). ``resolve_manifest`` fetches the manifest from a
tag-pinned URL, but a release asset or mirror that accidentally serves a manifest
for a *different* release would otherwise be installed silently: that manifest's
wheel URL and sha256 are internally consistent, so the checksum gate (which only
proves the wheel matches the manifest) cannot catch a wrong-but-coherent manifest.
The installer therefore cross-checks the manifest's own ``version`` and
``source.tag`` against the requested ``--version`` before downloading. A manifest
that omits those fields (legacy/hand-authored) is not enforced, mirroring how
``verify_channel`` tolerates a missing channel.
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_pinned_version_matching_manifest_installs(harness: InstallerHarness) -> None:
    """A pinned ``--version`` whose manifest declares the same version installs."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run(["--version", "0.1.0"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_pinned_version_mismatched_manifest_version_aborts(harness: InstallerHarness) -> None:
    """A manifest declaring a different ``version`` aborts before download/install."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    # The pin is 0.1.0 but the resolved manifest is a fully coherent 0.2.0 release
    # (its own wheel name + sha256 + URLs all match 0.2.0). The checksum gate would
    # pass; only the version cross-check catches the misattached/stale manifest.
    wheel, digest = harness.write_wheel(version="0.2.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.2.0")

    result = harness.run(["--version", "0.1.0"], manifest=manifest)

    assert result.returncode != 0
    assert "VERSION_MISMATCH" in result.stderr
    # Both the requested pin and the resolved version are surfaced for diagnosis.
    assert "0.1.0" in result.stderr
    assert "0.2.0" in result.stderr
    # Abort before the checksum gate and any install mutation.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_pinned_version_mismatched_manifest_tag_aborts(harness: InstallerHarness) -> None:
    """A manifest whose ``source.tag`` disagrees with the pin aborts before install."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    # The version field matches the pin, but source.tag attributes the manifest to
    # a different release tag — an inconsistent/tampered manifest the tag
    # cross-check must reject even though the version field looks right.
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0", tag="v0.2.0")

    result = harness.run(["--version", "0.1.0"], manifest=manifest)

    assert result.returncode != 0
    assert "VERSION_MISMATCH" in result.stderr
    assert "v0.2.0" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())
