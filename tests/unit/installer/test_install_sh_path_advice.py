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
def test_bash_path_advice_includes_login_profile_when_present(
    harness: InstallerHarness,
) -> None:
    """Bash advice also points at an existing login profile, not just ~/.bashrc.

    Login bash shells (macOS Terminal, SSH) read the first existing login
    profile (~/.bash_profile, ~/.bash_login, ~/.profile) and may not source
    ~/.bashrc, so a user following ~/.bashrc-only advice could still miss awf on
    PATH. When such a file exists, surface it alongside ~/.bashrc so both
    login and non-login shells pick up the export.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    (harness.home / ".bash_profile").write_text("# login profile\n", encoding="utf-8")
    install_dir = harness.root / "bin-install"
    install_dir.mkdir()
    harness.add_awf(directory=install_dir)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--install-dir", str(install_dir), "--shell", "bash"],
        manifest=manifest,
    )

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    # Both the login profile and ~/.bashrc are surfaced, plus the export line.
    assert ".bash_profile" in advice
    assert ".bashrc" in advice
    assert "export PATH=" in advice
    assert str(install_dir) in advice


@pytest.mark.unit
def test_fish_path_advice_quotes_bindir_with_spaces(harness: InstallerHarness) -> None:
    """Fish advice quotes a bindir containing whitespace so it stays one path.

    fish's ``fish_add_path`` is word-splitting like any fish command, so an
    unquoted ``fish_add_path /Users/me/Library/Application Support/awf/bin``
    pasted by the user would register three separate paths and never make awf
    reachable. zsh/bash already wrap the dir in double quotes; the fish line must
    quote it too so a spaced --install-dir survives copy-paste as a single path.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    install_dir = harness.root / "bin install"  # whitespace in the path
    install_dir.mkdir()
    harness.add_awf(directory=install_dir)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--install-dir", str(install_dir), "--shell", "fish"],
        manifest=manifest,
    )

    assert result.returncode == 0, result.stderr
    advice = result.stdout + result.stderr
    # The emitted line must quote the directory so copy-paste keeps it one arg.
    assert f'fish_add_path "{install_dir}"' in advice


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
    # When awf is already reachable, no PATH-remediation advice of any shell
    # flavour should be emitted — not just the zsh rc file.
    advice = result.stdout + result.stderr
    assert ".zshrc" not in advice
    assert ".bashrc" not in advice
    assert "config.fish" not in advice
    assert "export PATH=" not in advice
    assert "fish_add_path" not in advice
