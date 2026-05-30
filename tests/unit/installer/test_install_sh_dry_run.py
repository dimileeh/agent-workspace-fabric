"""Dry-run tests: planned actions are explained without install mutation."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


def _ordered(haystack: str, needles: list[str]) -> bool:
    """Return whether each needle appears after the previous one in ``haystack``."""
    index = 0
    for needle in needles:
        found = haystack.find(needle, index)
        if found < 0:
            return False
        index = found + len(needle)
    return True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("system", "machine"),
    [("Darwin", "arm64"), ("Linux", "x86_64")],
)
def test_dry_run_explains_ordered_plan_without_mutation(
    harness: InstallerHarness,
    system: str,
    machine: str,
) -> None:
    """Dry-run resolves/verifies and prints ordered actions, never installing."""
    harness.add_uname(system, machine)
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--dry-run"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert _ordered(
        result.stdout,
        ["resolve", "download", "sha256", "install", "awf"],
    ), result.stdout
    # No install method invoked and no shell rc files written under HOME.
    joined = "\n".join(harness.calls())
    assert "uv tool install" not in joined
    assert "pipx install" not in joined
    assert not list(harness.home.rglob("*rc"))
    assert not (harness.home / ".config" / "fish" / "config.fish").exists()


@pytest.mark.unit
def test_dry_run_reports_the_resolved_wheel_name(harness: InstallerHarness) -> None:
    """The dry-run plan references the manifest-pinned wheel artifact."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--dry-run"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert wheel.name in result.stdout


@pytest.mark.unit
def test_dry_run_uv_plan_shows_real_uv_tool_install_command(
    harness: InstallerHarness,
) -> None:
    """The uv dry-run plan prints the exact ``uv tool install`` command."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--dry-run", "--method", "uv"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert f"uv tool install {wheel.name}" in result.stdout


@pytest.mark.unit
def test_dry_run_pipx_plan_uses_valid_pipx_install_command(
    harness: InstallerHarness,
) -> None:
    """The pipx dry-run plan prints ``pipx install`` — pipx has no ``tool`` subcommand.

    A user reading the dry-run output to verify what would happen must see a
    runnable command; ``pipx tool install`` is not a real pipx invocation.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_pipx()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256=digest)

    result = harness.run(["--dry-run", "--method", "pipx"], manifest=manifest)

    assert result.returncode == 0, result.stderr
    assert f"pipx install {wheel.name}" in result.stdout
    # The invalid ``pipx tool install`` must never appear in the dry-run plan.
    assert "pipx tool install" not in result.stdout
