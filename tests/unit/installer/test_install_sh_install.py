"""Install-method, fallback, reachability, and dependency edge cases."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_install_method_failure_preserves_reason_and_tool_stderr(
    harness: InstallerHarness,
) -> None:
    """A failing ``uv tool install`` surfaces both the token and tool stderr."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(install_rc=7)
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "INSTALL_METHOD_FAILED" in result.stderr
    # The underlying tool failure must not be swallowed.
    assert "simulated install failure" in result.stderr


@pytest.mark.unit
def test_pipx_method_uses_pipx_not_uv(harness: InstallerHarness) -> None:
    """``--method pipx`` installs through pipx and never invokes uv install."""
    harness.add_uname("Darwin", "arm64")
    harness.add_uv()
    harness.add_pipx()
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--method", "pipx"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert "pipx install" in joined
    assert "uv tool install" not in joined


@pytest.mark.unit
def test_reachability_failure_does_not_claim_success(harness: InstallerHarness) -> None:
    """A successful install command with no runnable awf reports the failure."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds" but produces no awf binary
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
    # The exact shell fix must be printed so the user can recover.
    assert "export PATH=" in result.stderr


@pytest.mark.unit
def test_invalid_method_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An unsupported ``--method`` value is rejected before any work."""
    result = harness.run(["--method", "conda"])

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr


@pytest.mark.unit
def test_invalid_channel_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An unsupported ``--channel`` value is rejected before any work."""
    result = harness.run(["--channel", "nightly"])

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr


@pytest.mark.unit
def test_manifest_without_wheel_artifact_is_invalid(harness: InstallerHarness) -> None:
    """A manifest missing a wheel artifact fails with ``MANIFEST_INVALID``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    manifest = harness.root / "awf-install-manifest.json"
    manifest.write_text(
        '{\n  "artifacts": [],\n  "schema_version": 1,\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "MANIFEST_INVALID" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_missing_manifest_source_is_unavailable(harness: InstallerHarness) -> None:
    """A manifest path that does not exist fails with ``MANIFEST_UNAVAILABLE``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()

    result = harness.run([], manifest=harness.root / "missing-manifest.json")

    assert result.returncode != 0
    assert "MANIFEST_UNAVAILABLE" in result.stderr
