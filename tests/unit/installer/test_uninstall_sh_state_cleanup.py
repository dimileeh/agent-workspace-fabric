"""AWF state/config cleanup for ``packaging/uninstall.sh`` (T21).

``--purge-config`` and ``--purge-state`` remove only an explicit allowlist of
non-secret subpaths under ``${AWF_HOME}`` (``config.yml`` and ``service/``). They
preserve credentials by default — a seeded secret store and ``${AWF_HOME}`` itself
must survive every purge — and the destructive deletes require ``--yes`` or an
interactive confirmation, failing closed with ``CONFIRMATION_REQUIRED`` otherwise.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.installer.conftest import UNINSTALLER, InstallerHarness


def _env(harness: InstallerHarness) -> dict[str, str]:
    """The ``AWF_HOME`` seam so the uninstaller targets the hermetic state tree."""
    return {"AWF_HOME": str(harness.awf_home())}


def _run(
    harness: InstallerHarness, args: list[str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    extra_env = dict(_env(harness))
    extra_env.update(kwargs.pop("extra_env", {}))  # type: ignore[arg-type]
    return harness.run(args, script=UNINSTALLER, extra_env=extra_env, **kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_purge_config_with_yes_removes_only_config(harness: InstallerHarness) -> None:
    """``--purge-config --yes`` removes ``config.yml`` and nothing else."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    config = harness.write_awf_config()
    state = harness.write_awf_state()
    secret = harness.write_awf_secret()

    result = _run(harness, ["--purge-config", "--yes"])

    assert result.returncode == 0, result.stderr
    assert not config.exists()
    # Only config.yml is allowlisted for --purge-config; state and secrets survive.
    assert state.exists()
    assert secret.exists()
    assert harness.awf_home().exists()


@pytest.mark.unit
def test_purge_state_with_yes_removes_only_state(harness: InstallerHarness) -> None:
    """``--purge-state --yes`` removes the ``service/`` dir and nothing else."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    config = harness.write_awf_config()
    state = harness.write_awf_state()
    secret = harness.write_awf_secret()

    result = _run(harness, ["--purge-state", "--yes"])

    assert result.returncode == 0, result.stderr
    assert not state.exists()
    assert config.exists()
    assert secret.exists()
    assert harness.awf_home().exists()


@pytest.mark.unit
def test_all_purges_config_and_state_but_preserves_secret(
    harness: InstallerHarness,
) -> None:
    """``--all --yes`` purges config and state but never the secret or AWF_HOME."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    config = harness.write_awf_config()
    state = harness.write_awf_state()
    secret = harness.write_awf_secret()

    result = _run(harness, ["--all", "--yes"])

    assert result.returncode == 0, result.stderr
    assert not config.exists()
    assert not state.exists()
    # The credential-preservation default: secrets and the home dir outlive --all.
    assert secret.exists()
    assert harness.awf_home().exists()


@pytest.mark.unit
def test_purge_state_non_interactive_without_yes_requires_confirmation(
    harness: InstallerHarness,
) -> None:
    """Non-interactive without ``--yes`` fails closed and mutates nothing."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    state = harness.write_awf_state()

    result = _run(harness, ["--purge-state", "--non-interactive"])

    assert result.returncode != 0
    assert "CONFIRMATION_REQUIRED" in result.stderr
    # Fail-closed: the state dir is untouched.
    assert state.exists()


@pytest.mark.unit
def test_purge_config_default_non_tty_requires_confirmation(
    harness: InstallerHarness,
) -> None:
    """Even without ``--non-interactive``, a non-TTY run needs ``--yes``.

    The harness pipes ``/dev/null`` as stdin, so ``[ -t 0 ]`` is false and the
    destructive lane cannot prompt; without ``--yes`` it must fail closed rather
    than silently delete.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    config = harness.write_awf_config()

    result = _run(harness, ["--purge-config"])

    assert result.returncode != 0
    assert "CONFIRMATION_REQUIRED" in result.stderr
    assert config.exists()


@pytest.mark.unit
def test_purge_state_interactive_yes_removes(harness: InstallerHarness) -> None:
    """An affirmative interactive answer authorizes the state purge."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    state = harness.write_awf_state()

    result = _run(
        harness,
        ["--purge-state"],
        extra_env={"AWF_UNINSTALL_FORCE_INTERACTIVE": "1"},
        stdin="y\n",
    )

    assert result.returncode == 0, result.stderr
    assert not state.exists()


@pytest.mark.unit
def test_purge_state_interactive_decline_preserves(harness: InstallerHarness) -> None:
    """Declining the interactive prompt fails closed and preserves the state."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    state = harness.write_awf_state()

    result = _run(
        harness,
        ["--purge-state"],
        extra_env={"AWF_UNINSTALL_FORCE_INTERACTIVE": "1"},
        stdin="n\n",
    )

    assert result.returncode != 0
    assert "CONFIRMATION_REQUIRED" in result.stderr
    assert state.exists()


@pytest.mark.unit
def test_dry_run_plans_purge_without_mutation(harness: InstallerHarness) -> None:
    """``--dry-run`` plans the exact paths it would remove but mutates nothing."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    config = harness.write_awf_config()
    state = harness.write_awf_state()

    result = _run(harness, ["--all", "--dry-run"])

    assert result.returncode == 0, result.stderr
    # Both targets are still present after a dry run...
    assert config.exists()
    assert state.exists()
    # ...and the plan names them so the operator sees what a real run would delete.
    assert str(config) in result.stdout
    assert str(state) in result.stdout


@pytest.mark.unit
def test_purge_missing_target_is_a_noop(harness: InstallerHarness) -> None:
    """A purge for an absent target is a benign no-op (no confirmation needed)."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    # AWF_HOME exists but neither config.yml nor service/ is present.
    harness.awf_home().mkdir(parents=True, exist_ok=True)

    result = _run(harness, ["--all", "--non-interactive"])

    # No --yes, but nothing destructive happens, so no CONFIRMATION_REQUIRED.
    assert result.returncode == 0, result.stderr
    assert "CONFIRMATION_REQUIRED" not in result.stderr


@pytest.mark.unit
def test_unremovable_present_path_raises_state_cleanup_failed(
    harness: InstallerHarness,
) -> None:
    """An authorized removal that fails on a present path raises ``STATE_CLEANUP_FAILED``.

    Creating ``config.yml`` as a directory makes the uninstaller's ``rm -f`` (which
    targets it as a ``file`` kind) fail with "Is a directory", modeling a path that
    exists but cannot be removed. Unlike a ``chmod 0o500`` parent, this fails
    reliably under any user — including root, which bypasses directory write
    permissions (``CAP_DAC_OVERRIDE``) as many Docker-based CI runners do. The
    failure must surface non-zero with its token rather than be masked as success.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    # Create config.yml as a directory so the uninstaller's ``rm -f`` reliably fails.
    config = harness.awf_home() / "config.yml"
    config.mkdir(parents=True, exist_ok=True)

    result = _run(harness, ["--purge-config", "--yes"])

    assert result.returncode != 0
    assert "STATE_CLEANUP_FAILED" in result.stderr
    assert config.exists()


@pytest.mark.unit
def test_purge_uses_default_awf_home_under_real_home(
    harness: InstallerHarness,
) -> None:
    """Without the ``AWF_HOME`` seam, purge targets ``$HOME/.awf`` (production default).

    The seam is a test convenience; this proves the production default resolves
    to ``$HOME/.awf`` by seeding config there and purging without ``AWF_HOME``.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    config = harness.write_awf_config()  # lands in $HOME/.awf/config.yml
    assert config == harness.home / ".awf" / "config.yml"

    result = harness.run(["--purge-config", "--yes"], script=UNINSTALLER)

    assert result.returncode == 0, result.stderr
    assert not config.exists()
