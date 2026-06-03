"""Shared scaffolding for the ``awf setup`` CLI tests.

Extracted from ``test_setup_commands.py`` so the suite stays under the
first-party file line limit; both the runner handle and the hermetic
``harness`` fixture live here and are imported by the test module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awf.cli import setup_commands
from awf.host_setup.config import HostSetupConfig
from awf.host_setup.providers import (
    ProviderSetupResult,
    ProviderSetupSummary,
)
from awf.host_setup.rendering import INTERACTIVE_INPUT_REQUIRED
from awf.host_setup.source_assets import SOURCE_CHECKOUT_MARKERS
from awf.host_setup.system_checks import SetupCheckLevel, SetupCheckResult

_runner = CliRunner()

# Captured before the ``harness`` fixture stubs it so the env-merge regression
# tests can opt back into the real resolver.
_real_readiness_environ = setup_commands._readiness_environ


def _ok(name: str) -> SetupCheckResult:
    return SetupCheckResult(name=name, level=SetupCheckLevel.OK, summary="ok", detail="ok")


def _all_ok(**_kwargs: object) -> list[SetupCheckResult]:
    return [_ok("docker"), _ok("compose"), _ok("git")]


def _docker_blocked(**_kwargs: object) -> list[SetupCheckResult]:
    return [
        SetupCheckResult(
            name="docker",
            level=SetupCheckLevel.BLOCKED,
            summary="Docker is installed but the daemon is not reachable.",
            detail="`docker info` failed.",
            fix="Start Docker Desktop or the Docker daemon.",
            docs_link="https://docs.docker.com/config/daemon/",
            data={"daemon": False},
        ),
        SetupCheckResult(
            name="gh",
            level=SetupCheckLevel.WARNING,
            summary="GitHub CLI (gh) is not installed.",
            detail="gh missing.",
            fix="Install gh.",
        ),
    ]


def _gh_warning(**_kwargs: object) -> list[SetupCheckResult]:
    """An otherwise-ready host carrying a single non-blocking warning."""
    return [
        _ok("docker"),
        _ok("compose"),
        SetupCheckResult(
            name="gh",
            level=SetupCheckLevel.WARNING,
            summary="GitHub CLI (gh) is not installed.",
            detail="gh missing.",
            fix="Install gh.",
        ),
    ]


@dataclass
class _Harness:
    writes: list[HostSetupConfig] = field(default_factory=list)


def _fake_orchestrate(
    _settings: object,
    *,
    selected_providers: list[str],
    config: HostSetupConfig,
    non_interactive: bool,
    **_kwargs: object,
) -> tuple[ProviderSetupSummary, HostSetupConfig]:
    """Hermetic default orchestration: never probes, never mutates config.

    Selected providers report ``not_configured`` (interactive input required under
    ``--non-interactive``) so the existing provider-guard regression tests stay
    deterministic; an all-provider run reports ``all_providers`` with no
    interactive requirement. New tests override this with their own stub.
    """
    results = tuple(
        ProviderSetupResult(
            name=name,
            status="not_configured",
            reason_code=(INTERACTIVE_INPUT_REQUIRED if non_interactive else f"{name.upper()}_NA"),
            summary=f"{name} not configured in this hermetic run.",
        )
        for name in selected_providers
    )
    summary = ProviderSetupSummary(
        mode="targeted_recheck" if selected_providers else "all_providers",
        selected=tuple(selected_providers),
        providers=results,
        overall_status="not_ready",
    )
    return summary, config


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """Isolate config IO and default the host checks to all-OK."""
    state = _Harness()
    monkeypatch.setattr(setup_commands, "read_host_setup_config", lambda **_kw: HostSetupConfig())

    def fake_write(config: HostSetupConfig, **_kw: object) -> None:
        state.writes.append(config)

    monkeypatch.setattr(setup_commands, "write_host_setup_config", fake_write)
    monkeypatch.setattr(setup_commands, "run_system_checks", _all_ok)
    # Keep provider orchestration hermetic by default: never resolve real service
    # settings or probe gh/network. Tests that exercise orchestration override
    # ``orchestrate_provider_setup`` with their own fake summary.
    monkeypatch.setattr(setup_commands, "_resolve_provider_settings", lambda _environ: None)
    monkeypatch.setattr(setup_commands, "orchestrate_provider_setup", _fake_orchestrate)
    # Default to a hermetic, no-IO readiness environ. The real ``_readiness_environ``
    # resolves the bootstrap asset root and reads ``docker/compose/.env`` from disk on
    # every harness-based test, even though the stubbed ``run_system_checks`` ignores
    # the kwarg — pointless I/O coupled to bootstrap-asset resolution. The env-merge
    # regression tests re-enable the real resolver explicitly via
    # ``_real_readiness_environ``.
    monkeypatch.setattr(setup_commands, "_readiness_environ", lambda *_a: {})
    return state


def _make_source_checkout(root: Path) -> Path:
    """Materialize every required AWF source-checkout marker under ``root``."""
    for marker in SOURCE_CHECKOUT_MARKERS:
        target = root / marker.path
        if marker.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
    return root
