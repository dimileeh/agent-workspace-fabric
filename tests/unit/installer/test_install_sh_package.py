"""The resolved manifest's package identity must match the installer's PACKAGE.

``packaging/install.sh`` installs the ``agent-workspace-fabric`` distribution. It
resolves a manifest (from a release asset or the ``AWF_INSTALL_BASE_URL`` mirror),
reads the pinned wheel's name/url/sha256 from it, and installs that wheel via
uv/pipx. Those artifact fields are internally consistent for *whatever* package
the manifest describes, and the checksum gate only proves the wheel matches the
manifest -- not that the manifest is for ``agent-workspace-fabric``. A release
asset or mirror that accidentally serves a manifest for a *different* package
sharing this version/tag would therefore mutate the user's environment with the
wrong tool. ``verify_package`` cross-checks the manifest's own top-level
``package`` field against ``PACKAGE`` before downloading. A manifest that omits
the field (legacy/hand-authored) is not enforced, mirroring how ``verify_channel``
and ``verify_version`` tolerate a missing field.
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_package_mismatch_aborts_before_install(harness: InstallerHarness) -> None:
    """A manifest attributed to a different package aborts before download/install."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    # A fully coherent manifest (its wheel name + sha256 + url all match) that is
    # nonetheless attributed to a different package. The checksum gate would pass;
    # only the package cross-check catches the misattached manifest.
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, package="some-other-tool")

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "PACKAGE_MISMATCH" in result.stderr
    # Both the manifest's package and the expected package are surfaced for diagnosis.
    assert "some-other-tool" in result.stderr
    assert "agent-workspace-fabric" in result.stderr
    # Abort before the checksum gate and any install mutation.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_matching_package_installs(harness: InstallerHarness) -> None:
    """A manifest whose ``package`` matches ``PACKAGE`` installs unchanged."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, package="agent-workspace-fabric")

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "PACKAGE_MISMATCH" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_missing_package_field_is_tolerated(harness: InstallerHarness) -> None:
    """A manifest that omits ``package`` installs (legacy/hand-authored manifest)."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, package=None)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "PACKAGE_MISMATCH" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())
