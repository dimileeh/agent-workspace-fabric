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
def test_default_install_bin_off_path_is_reachable_with_advice(
    harness: InstallerHarness,
) -> None:
    """A default install into ~/.local/bin off PATH succeeds with PATH advice.

    With no ``--install-dir``, uv/pipx install into ``~/.local/bin``. When that
    directory is not on PATH the install is still valid: reachability must fall
    back to the default bin dir and succeed with PATH advice, not fail with
    ``AWF_NOT_REACHABLE``.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds"
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin)  # awf lands in the default bin, off PATH
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr
    # The user still gets actionable PATH advice for the off-PATH default bin.
    assert "export PATH=" in result.stderr
    assert str(default_bin) in result.stderr


@pytest.mark.unit
def test_default_install_verifies_installed_binary_not_path_shadow(
    harness: InstallerHarness,
) -> None:
    """A default install must not let an older PATH awf shadow verification.

    With no ``--install-dir``, uv/pipx install into ``~/.local/bin``. When an
    unrelated awf sits earlier on PATH, verification must inspect the freshly
    installed binary in the default bin dir and still surface PATH advice for it,
    not silently pass on the shadowing binary and report success without advice.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()  # older, unrelated awf earlier on PATH (stub bin dir)
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin)  # freshly installed binary, off PATH
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    assert "export PATH=" in advice
    assert str(default_bin) in advice


@pytest.mark.unit
def test_default_install_binary_verified_even_when_path_awf_is_broken(
    harness: InstallerHarness,
) -> None:
    """The default-bin binary is verified, not a broken awf earlier on PATH.

    A non-runnable awf on PATH must not make verification fail when the freshly
    installed ``~/.local/bin/awf`` is itself runnable.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf(rc=1)  # broken awf earlier on PATH
    default_bin = harness.home / ".local" / "bin"
    default_bin.mkdir(parents=True)
    harness.add_awf(directory=default_bin)  # runnable freshly installed binary
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr


@pytest.mark.unit
def test_install_dir_verifies_installed_binary_not_path_shadow(
    harness: InstallerHarness,
) -> None:
    """With --install-dir, an older awf on PATH must not shadow verification.

    The freshly installed binary lives at ``${INSTALL_DIR}/awf`` (off PATH) while
    an unrelated awf sits earlier on PATH. Verification must inspect the install
    location and still surface PATH advice for it, not silently pass on the
    shadowing binary and report success without advice.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()  # older, unrelated awf earlier on PATH (stub bin dir)
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    harness.add_awf(directory=install_dir)  # freshly installed binary, off PATH
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    assert "export PATH=" in advice
    assert str(install_dir) in advice


@pytest.mark.unit
def test_install_dir_binary_verified_even_when_path_awf_is_broken(
    harness: InstallerHarness,
) -> None:
    """The install-dir binary is verified, not a broken awf earlier on PATH.

    A non-runnable awf on PATH must not make verification fail when the freshly
    installed ``${INSTALL_DIR}/awf`` is itself runnable.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf(rc=1)  # broken awf earlier on PATH
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    harness.add_awf(directory=install_dir)  # runnable freshly installed binary
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert "AWF_NOT_REACHABLE" not in result.stderr


@pytest.mark.unit
def test_install_dir_missing_binary_fails_even_with_stale_path_awf(
    harness: InstallerHarness,
) -> None:
    """A stale PATH awf cannot mask a missing install-dir binary.

    When ``--install-dir`` produced no awf, verification must fail with
    ``AWF_NOT_REACHABLE`` rather than falsely passing on an unrelated awf that
    happens to be earlier on PATH.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()  # install "succeeds" but produces no awf in the install dir
    harness.add_awf()  # unrelated awf earlier on PATH
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--install-dir", str(install_dir)], manifest=manifest)

    assert result.returncode != 0
    assert "AWF_NOT_REACHABLE" in result.stderr
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
