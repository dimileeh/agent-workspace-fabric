"""The manifest's selected wheel *artifact* must itself be a wheel for PACKAGE.

``packaging/install.sh`` resolves a manifest, reads the pinned wheel's
name/url/sha256 from it, and installs that wheel via uv/pipx. ``parse_manifest``
only proves the wheel artifact's ``name`` is a safe plain basename, and
``verify_package``/``verify_version`` only cross-check the manifest's *top-level*
``package``/``version`` fields. A misattached or hand-crafted manifest can still
pair a top-level ``package=agent-workspace-fabric`` / ``version=X`` with a wheel
artifact for a *different* distribution or version (for example
``other_tool-0.1.0-py3-none-any.whl`` with a matching sha256). The checksum gate
only proves the downloaded bytes match that artifact -- not that the artifact is a
wheel for ``agent-workspace-fabric`` at the declared version -- so without this
check the installer would download that wheel and hand it to uv/pipx, mutating the
environment with the wrong package before reachability fails (or even reporting
success if the wheel exposes an ``awf`` command).

``verify_artifact_name`` therefore validates the wheel filename's own distribution
and version components before download/install, mirroring the manifest generator's
``_validate_distribution_metadata`` (``scripts/generate_install_manifest.py``) so
the installer accepts exactly the escaped wheel names a real release emits. When a
manifest omits its top-level ``version`` (legacy/hand-authored) the wheel filename is
still cross-checked against a pinned ``--version`` -- the documented trust boundary --
so an incomplete manifest cannot smuggle a different release past the pin; only an
unpinned install with no declared version is left unenforced (nothing to compare).
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_wheel_artifact_for_other_distribution_aborts_before_install(
    harness: InstallerHarness,
) -> None:
    """A wheel artifact whose distribution is not AWF aborts before download/install."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    # A fully coherent manifest: its top-level ``package`` is AWF (so verify_package
    # passes) and the wheel url/sha256 match the real fixture bytes (so the checksum
    # gate would pass). Only the wheel *filename* gives it away as another package.
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_name="other_tool-0.1.0-py3-none-any.whl",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "PACKAGE_MISMATCH" in result.stderr
    # Both the wheel's distribution and the expected package are surfaced.
    assert "other_tool" in result.stderr
    assert "agent-workspace-fabric" in result.stderr
    # Abort before the checksum gate and any install mutation.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_wheel_artifact_for_other_version_aborts_before_install(
    harness: InstallerHarness,
) -> None:
    """A wheel artifact whose version disagrees with the manifest aborts before install."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    # The distribution matches AWF but the wheel filename's version (9.9.9) is not
    # the version the manifest declares (0.1.0) -- a misattached wheel the version
    # cross-check must reject even though it is genuinely an AWF wheel name.
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_name="agent_workspace_fabric-9.9.9-py3-none-any.whl",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "VERSION_MISMATCH" in result.stderr
    # Both the wheel's version and the manifest's declared version are surfaced.
    assert "9.9.9" in result.stderr
    assert "0.1.0" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_non_wheel_artifact_name_is_invalid(harness: InstallerHarness) -> None:
    """A wheel artifact name that is not a ``.whl`` file fails with ``MANIFEST_INVALID``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    # A plain basename (so parse_manifest's traversal guard passes) that is not a
    # wheel filename at all. The installer cannot reason about its distribution or
    # version, so it must refuse rather than install an unidentifiable artifact.
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_name="agent_workspace_fabric-0.1.0",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "MANIFEST_INVALID" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_matching_wheel_artifact_installs(harness: InstallerHarness) -> None:
    """The canonical escaped wheel name (``agent_workspace_fabric-...``) installs.

    Wheel filenames escape the distribution name (``agent-workspace-fabric`` ->
    ``agent_workspace_fabric``), so the check must normalize both sides with the
    same PEP 503/427 rules the generator uses rather than demand a literal match.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "PACKAGE_MISMATCH" not in result.stderr
    assert "VERSION_MISMATCH" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_wheel_artifact_with_build_tag_installs(harness: InstallerHarness) -> None:
    """A 6-component wheel name (optional build tag) is accepted, not over-rejected.

    PEP 427 allows an optional build-tag component between version and python tag.
    The distribution/version arms must read the first two fields and ignore the
    rest so a legitimately built wheel is not refused.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_name="agent_workspace_fabric-0.1.0-1-py3-none-any.whl",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "PACKAGE_MISMATCH" not in result.stderr
    assert "VERSION_MISMATCH" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_wheel_artifact_check_applies_under_dry_run(harness: InstallerHarness) -> None:
    """A wrong-distribution wheel is refused even under ``--dry-run``.

    The artifact identity check runs before the download/dry-run branch, so a
    dry-run preview of a misattached manifest aborts rather than reporting a clean
    plan for the wrong package.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_name="other_tool-0.1.0-py3-none-any.whl",
    )

    result = harness.run(["--dry-run"], manifest=manifest)

    assert result.returncode != 0
    assert "PACKAGE_MISMATCH" in result.stderr
    assert "Dry run complete" not in result.stdout


@pytest.mark.unit
def test_pinned_version_missing_manifest_version_rejects_mismatched_wheel(
    harness: InstallerHarness,
) -> None:
    """A versionless manifest cannot smuggle a different release past a ``--version`` pin.

    The caller pins ``--version 0.1.0`` but the resolved manifest omits both its
    top-level ``version`` and ``source.tag`` (a legacy/hand-authored shape), so
    ``verify_version`` finds nothing to enforce. Its wheel artifact is a fully
    coherent 0.2.0 release (its own name + sha256 match), so the checksum gate would
    pass. ``--version`` is the documented trust boundary, so the artifact-name check
    must fall back to comparing the wheel's own version against the pin rather than
    returning early — otherwise the pin silently installs 0.2.0.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel(version="0.2.0")
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        version="0.2.0",
        include_version=False,
        include_tag=False,
    )

    result = harness.run(["--version", "0.1.0"], manifest=manifest)

    assert result.returncode != 0
    assert "VERSION_MISMATCH" in result.stderr
    # Both the wheel's version and the requested pin are surfaced for diagnosis.
    assert "0.2.0" in result.stderr
    assert "0.1.0" in result.stderr
    # Abort before the checksum gate and any install mutation.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_pinned_version_missing_manifest_version_accepts_matching_wheel(
    harness: InstallerHarness,
) -> None:
    """A versionless manifest whose wheel matches the pin still installs.

    The legacy/hand-authored manifest omits ``version`` and ``source.tag``, but its
    wheel filename is for exactly the pinned ``0.1.0`` release. The pin-fallback in
    the artifact-name check must accept it rather than over-reject a legitimate
    versionless manifest.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        version="0.1.0",
        include_version=False,
        include_tag=False,
    )

    result = harness.run(["--version", "0.1.0"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "VERSION_MISMATCH" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_unpinned_missing_manifest_version_accepts_wheel(harness: InstallerHarness) -> None:
    """An unpinned install with a versionless manifest is left unenforced.

    Without a ``--version`` pin and without a declared manifest version there is
    nothing to compare the wheel version against, so the artifact-name check accepts
    the wheel as the version of record — preserving the deliberate tolerance of
    legacy/hand-authored manifests and not over-tightening unpinned installs.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    wheel, digest = harness.write_wheel(version="0.2.0")
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        version="0.2.0",
        include_version=False,
        include_tag=False,
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "VERSION_MISMATCH" not in result.stderr
    assert "uv tool install" in "\n".join(harness.calls())
