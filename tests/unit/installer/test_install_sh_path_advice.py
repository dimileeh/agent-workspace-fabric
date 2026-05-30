"""Per-shell PATH advice is correct when the install dir is not on PATH."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shell", "rc_fragment", "syntax_fragment"),
    [
        ("zsh", ".zshrc", "export PATH="),
        ("bash", ".bashrc", "export PATH="),
        ("fish", "config.fish", "fish_add_path"),
    ],
)
def test_path_advice_matches_shell(
    harness: InstallerHarness,
    shell: str,
    rc_fragment: str,
    syntax_fragment: str,
) -> None:
    """A successful install off-PATH prints rc + export advice per shell."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    # awf is installed into install_dir but install_dir is not on PATH.
    harness.add_awf(directory=install_dir)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--install-dir", str(install_dir), "--shell", shell],
        manifest=manifest,
    )

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    assert rc_fragment in advice
    assert syntax_fragment in advice
    assert str(install_dir) in advice


@pytest.mark.unit
def test_no_path_advice_when_awf_already_on_path(harness: InstallerHarness) -> None:
    """When awf resolves on PATH, no PATH remediation advice is emitted."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()  # on PATH via the stub bin dir
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--shell", "zsh"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert ".zshrc" not in (result.stdout + result.stderr)
