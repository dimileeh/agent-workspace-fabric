"""Explicit uv-bootstrap contract for ``packaging/install.sh`` (T20).

``--method uv`` requires ``uv``. Historically a missing ``uv`` aborted late with
``MISSING_DEPENDENCY``. T20 adds a deliberate bootstrap contract:

* ``uv`` present  -> existing behavior is preserved (no bootstrap).
* ``--method pipx`` -> never bootstraps ``uv``.
* ``uv`` missing, non-interactive, no opt-in -> ``UV_BOOTSTRAP_REQUIRED``.
* ``--bootstrap-uv`` -> explicit opt-in installs ``uv`` via the official installer.
* interactive TTY  -> prompt, proceed only on an affirmative answer.
* ``--dry-run``    -> the bootstrap is planned, never executed.

All cases run hermetically: the uv installer is the ``AWF_UV_INSTALLER`` seam
(a local fixture script) and the prompt path uses the
``AWF_INSTALL_FORCE_INTERACTIVE`` seam, so no real network or release is touched.
Each case asserts on stable shell reason tokens and recorded stub ``calls()`` /
filesystem effects, never on incidental wording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.installer.conftest import InstallerHarness


def _bootstrapped_uv(harness: InstallerHarness) -> bool:
    """Whether a uv stub was dropped into the bootstrap target (``~/.local/bin``)."""
    return (harness.home / ".local" / "bin" / "uv").exists()


def _uv_marker(harness: InstallerHarness) -> Path:
    """The AWF uv-ownership marker path ``bootstrap_uv`` writes (``~/.awf/...``)."""
    return harness.home / ".awf" / "uv-bootstrap.marker"


@pytest.mark.unit
def test_uv_present_does_not_bootstrap(harness: InstallerHarness) -> None:
    """With ``uv`` already installed the install path is unchanged: no bootstrap."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    installer = harness.write_uv_installer()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)})

    assert result.returncode == 0, result.stderr
    # No bootstrap was planned or run, and the install proceeded as before.
    assert "bootstrap uv" not in result.stdout
    assert "uv tool install" in "\n".join(harness.calls())
    # The seam installer was never invoked (it would have dropped uv into the
    # bootstrap target).
    assert not _bootstrapped_uv(harness)


@pytest.mark.unit
def test_bootstrap_uv_is_a_noop_when_uv_already_present(harness: InstallerHarness) -> None:
    """``--bootstrap-uv`` with ``uv`` present installs nothing extra."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    harness.add_awf()
    installer = harness.write_uv_installer()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--bootstrap-uv"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode == 0, result.stderr
    assert "bootstrap uv" not in result.stdout
    assert "uv tool install" in "\n".join(harness.calls())
    assert not _bootstrapped_uv(harness)


@pytest.mark.unit
def test_pipx_method_never_bootstraps_uv(harness: InstallerHarness) -> None:
    """``--method pipx`` installs via pipx even with ``uv`` missing — no bootstrap."""
    harness.add_uname("Linux", "x86_64")
    harness.add_pipx()  # uv is intentionally absent
    harness.add_awf()
    installer = harness.write_uv_installer()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--method", "pipx"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert "pipx install" in joined
    assert "uv tool install" not in joined
    assert "bootstrap uv" not in result.stdout
    assert not _bootstrapped_uv(harness)


@pytest.mark.unit
def test_uv_missing_non_interactive_requires_explicit_opt_in(harness: InstallerHarness) -> None:
    """``uv`` missing, non-interactive, no opt-in -> ``UV_BOOTSTRAP_REQUIRED``.

    The failure must name all three recoveries so the operator can act.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_awf()  # uv intentionally absent
    installer = harness.write_uv_installer()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run([], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)})

    assert result.returncode != 0
    assert "UV_BOOTSTRAP_REQUIRED" in result.stderr
    # Names the three recoveries: opt-in flag, manual install, pipx fallback.
    assert "--bootstrap-uv" in result.stderr
    assert "--method pipx" in result.stderr
    # No install ran and nothing was bootstrapped.
    assert "uv tool install" not in "\n".join(harness.calls())
    assert not _bootstrapped_uv(harness)


@pytest.mark.unit
def test_bootstrap_uv_opt_in_installs_uv_then_awf(harness: InstallerHarness) -> None:
    """``--bootstrap-uv`` installs ``uv`` via the seam, then proceeds to install awf."""
    harness.add_uname("Linux", "x86_64")
    harness.add_awf(version="0.1.0")  # uv absent until the bootstrap drops it
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run(
        ["--bootstrap-uv"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode == 0, result.stderr
    assert "Bootstrapped uv" in result.stdout
    # The seam installer actually ran (its sentinel proves invocation)...
    assert "uv-installer" in "\n".join(harness.calls())
    # ...the bootstrap honored UV_INSTALL_DIR: the uv stub landed in ~/.local/bin...
    assert _bootstrapped_uv(harness)
    # ...and the freshly bootstrapped uv carried the real install through.
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_real_bootstrap_writes_uv_ownership_marker(harness: InstallerHarness) -> None:
    """A real ``--bootstrap-uv`` writes the AWF uv-ownership marker.

    The marker is the producer half of the T21 uninstaller's marker-gated
    ``--remove-uv``: only a uv AWF actually bootstrapped is later removable. It is
    deterministic (no timestamp) and carries the ``installed_by=awf`` line.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_awf(version="0.1.0")
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run(
        ["--bootstrap-uv"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode == 0, result.stderr
    marker = _uv_marker(harness)
    assert marker.exists()
    assert "installed_by=awf" in marker.read_text(encoding="utf-8")


@pytest.mark.unit
def test_real_bootstrap_marker_follows_custom_awf_home(harness: InstallerHarness) -> None:
    """The ownership marker default tracks a custom ``AWF_HOME`` (symmetric with uninstall.sh).

    ``bootstrap_uv`` writes the marker at ``${AWF_HOME}/uv-bootstrap.marker``. With
    only ``AWF_HOME`` customised (no ``AWF_UV_MARKER`` seam) the marker must land
    under the custom home, not the hardcoded ``~/.awf``, so the hosted uninstaller —
    which derives the same default — finds it for marker-gated ``--remove-uv``.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_awf(version="0.1.0")
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")
    custom_home = harness.home / "custom-awf"

    result = harness.run(
        ["--bootstrap-uv"],
        manifest=manifest,
        extra_env={"AWF_UV_INSTALLER": str(installer), "AWF_HOME": str(custom_home)},
    )

    assert result.returncode == 0, result.stderr
    marker = custom_home / "uv-bootstrap.marker"
    assert marker.exists()
    assert "installed_by=awf" in marker.read_text(encoding="utf-8")
    # The hardcoded ~/.awf default must NOT have been used.
    assert not _uv_marker(harness).exists()


@pytest.mark.unit
def test_bootstrap_does_not_claim_preexisting_uv_in_default_dir(harness: InstallerHarness) -> None:
    """A bootstrap that refreshes a preexisting uv must NOT write the ownership marker.

    uv can already live in the default ``~/.local/bin`` yet be absent from ``PATH``
    (profile not sourced in this shell), so ``prepare_install_method``'s
    ``command -v uv`` misses and routes into ``bootstrap_uv``. The binaries are the
    user's, not AWF's. Writing the marker would let a later
    ``uninstall.sh --remove-uv`` delete a uv the user installed themselves, breaking
    the "uv you installed yourself is never removed" contract — so no marker is
    written and the install still completes.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_awf(version="0.1.0")
    # Seed a preexisting uv in the default bootstrap dir WITHOUT putting it on PATH,
    # so command -v uv misses and the bootstrap lane still runs over it.
    preexisting = harness.home / ".local" / "bin" / "uv"
    preexisting.parent.mkdir(parents=True, exist_ok=True)
    preexisting.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    preexisting.chmod(0o755)
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run(
        ["--bootstrap-uv"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode == 0, result.stderr
    # The bootstrap actually ran (the seam installer was invoked)...
    assert "uv-installer" in "\n".join(harness.calls())
    # ...the install proceeded via the resolved uv...
    assert "uv tool install" in "\n".join(harness.calls())
    # ...but AWF did not claim ownership of the user's preexisting uv.
    assert not _uv_marker(harness).exists()


@pytest.mark.unit
def test_bootstrap_dry_run_writes_no_marker(harness: InstallerHarness) -> None:
    """``--bootstrap-uv --dry-run`` plans the bootstrap and writes no marker."""
    harness.add_uname("Linux", "x86_64")
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--bootstrap-uv", "--dry-run"],
        manifest=manifest,
        extra_env={"AWF_UV_INSTALLER": str(installer)},
    )

    assert result.returncode == 0, result.stderr
    # No real bootstrap ran, so no ownership marker was written.
    assert not _uv_marker(harness).exists()


@pytest.mark.unit
def test_bootstrap_disables_uv_installer_shell_modifications(harness: InstallerHarness) -> None:
    """The bootstrap runs the uv installer with ``UV_NO_MODIFY_PATH=1``.

    The official uv installer mutates shell profiles (``.bashrc``/``.zshrc``) by
    default unless ``UV_NO_MODIFY_PATH=1`` is set. AWF owns PATH guidance — it
    prepends the bootstrap dir to ``PATH`` for this process and prints its own
    advice — so the installer must not also rewrite the user's shell config.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_awf(version="0.1.0")
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run(
        ["--bootstrap-uv"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode == 0, result.stderr
    # The seam installer ran, and every invocation saw UV_NO_MODIFY_PATH=1 — proving
    # the bootstrap suppressed the installer's shell-profile edits.
    installer_calls = [line for line in harness.calls() if line.startswith("uv-installer ")]
    assert installer_calls, harness.calls()
    assert all("UV_NO_MODIFY_PATH=1" in line for line in installer_calls)


@pytest.mark.unit
def test_bootstrap_uv_dry_run_plans_without_mutation(harness: InstallerHarness) -> None:
    """``--bootstrap-uv --dry-run`` plans the bootstrap but never executes it."""
    harness.add_uname("Linux", "x86_64")
    installer = harness.write_uv_installer(succeeds=True)  # must NOT run
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--bootstrap-uv", "--dry-run"],
        manifest=manifest,
        extra_env={"AWF_UV_INSTALLER": str(installer)},
    )

    assert result.returncode == 0, result.stderr
    # The bootstrap is planned...
    assert "bootstrap uv" in result.stdout
    assert "Dry run complete" in result.stdout
    # ...but the seam installer never ran (its sentinel is absent, so the proof
    # holds even if a future helper variant exited before dropping the binary)
    # and nothing was installed.
    assert "uv-installer" not in "\n".join(harness.calls())
    assert not _bootstrapped_uv(harness)
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_uv_missing_dry_run_without_opt_in_still_requires_bootstrap(
    harness: InstallerHarness,
) -> None:
    """``--dry-run`` with ``uv`` missing and no opt-in fails clearly, non-mutating."""
    harness.add_uname("Linux", "x86_64")
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--dry-run"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode != 0
    assert "UV_BOOTSTRAP_REQUIRED" in result.stderr
    assert not _bootstrapped_uv(harness)
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_interactive_confirm_bootstraps_uv(harness: InstallerHarness) -> None:
    """On a TTY, an affirmative prompt answer confirms the bootstrap."""
    harness.add_uname("Linux", "x86_64")
    harness.add_awf(version="0.1.0")
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel(version="0.1.0")
    manifest = harness.write_manifest(wheel=wheel, sha256=digest, version="0.1.0")

    result = harness.run(
        [],
        manifest=manifest,
        extra_env={
            "AWF_UV_INSTALLER": str(installer),
            "AWF_INSTALL_FORCE_INTERACTIVE": "1",
        },
        stdin="y\n",
    )

    assert result.returncode == 0, result.stderr
    # The user was actually prompted before the bootstrap proceeded.
    assert "Install uv now" in result.stderr
    assert "Bootstrapped uv" in result.stdout
    assert _bootstrapped_uv(harness)
    assert "uv tool install" in "\n".join(harness.calls())


@pytest.mark.unit
def test_interactive_decline_requires_bootstrap(harness: InstallerHarness) -> None:
    """Declining the prompt falls through to ``UV_BOOTSTRAP_REQUIRED``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_awf()
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        [],
        manifest=manifest,
        extra_env={
            "AWF_UV_INSTALLER": str(installer),
            "AWF_INSTALL_FORCE_INTERACTIVE": "1",
        },
        stdin="n\n",
    )

    assert result.returncode != 0
    assert "Install uv now" in result.stderr  # the prompt was shown
    assert "UV_BOOTSTRAP_REQUIRED" in result.stderr
    assert not _bootstrapped_uv(harness)
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_non_interactive_flag_overrides_prompt_seam(harness: InstallerHarness) -> None:
    """``--non-interactive`` wins over the force-interactive seam: never prompts.

    Even with the prompt seam set and an affirmative ``stdin``, ``--non-interactive``
    forces the non-interactive decision, so a missing ``uv`` without ``--bootstrap-uv``
    fails with ``UV_BOOTSTRAP_REQUIRED`` and no prompt is emitted.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_awf()
    installer = harness.write_uv_installer(succeeds=True)
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--non-interactive"],
        manifest=manifest,
        extra_env={
            "AWF_UV_INSTALLER": str(installer),
            "AWF_INSTALL_FORCE_INTERACTIVE": "1",
        },
        stdin="y\n",
    )

    assert result.returncode != 0
    assert "UV_BOOTSTRAP_REQUIRED" in result.stderr
    # Never prompted, never bootstrapped, never installed.
    assert "Install uv now" not in result.stderr
    assert not _bootstrapped_uv(harness)
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_bootstrap_download_failure_is_reported(harness: InstallerHarness) -> None:
    """A uv installer that cannot be fetched fails with ``UV_BOOTSTRAP_FAILED``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_awf()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)
    missing_installer = harness.root / "does-not-exist-uv-installer.sh"

    result = harness.run(
        ["--bootstrap-uv"],
        manifest=manifest,
        extra_env={"AWF_UV_INSTALLER": str(missing_installer)},
    )

    assert result.returncode != 0
    assert "UV_BOOTSTRAP_FAILED" in result.stderr
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_bootstrap_that_yields_no_uv_is_reported(harness: InstallerHarness) -> None:
    """A bootstrap that runs but installs no ``uv`` fails with ``UV_BOOTSTRAP_FAILED``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_awf()
    installer = harness.write_uv_installer(succeeds=False)  # runs, installs nothing
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(
        ["--bootstrap-uv"], manifest=manifest, extra_env={"AWF_UV_INSTALLER": str(installer)}
    )

    assert result.returncode != 0
    assert "UV_BOOTSTRAP_FAILED" in result.stderr
    assert not _bootstrapped_uv(harness)
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_bootstrap_uv_with_pipx_method_is_bad_usage(harness: InstallerHarness) -> None:
    """``--bootstrap-uv --method pipx`` is rejected at parse time (pipx never bootstraps)."""
    harness.add_uname("Linux", "x86_64")

    result = harness.run(["--bootstrap-uv", "--method", "pipx"])

    assert result.returncode == 2, result.stderr
    assert "BAD_USAGE" in result.stderr
    assert "--bootstrap-uv" in result.stderr
    # Rejected during arg parsing: no platform probe or tool invocation.
    assert harness.calls() == []


@pytest.mark.unit
def test_bootstrap_uv_with_uninstall_is_bad_usage(harness: InstallerHarness) -> None:
    """``--bootstrap-uv --uninstall`` is rejected: bootstrap is an install-only flag."""
    harness.add_uname("Linux", "x86_64")

    result = harness.run(["--bootstrap-uv", "--uninstall"])

    assert result.returncode == 2, result.stderr
    assert "BAD_USAGE" in result.stderr
    assert "--bootstrap-uv" in result.stderr
    assert harness.calls() == []
