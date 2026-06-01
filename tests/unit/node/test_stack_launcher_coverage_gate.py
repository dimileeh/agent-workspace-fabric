"""Targeted coverage for ``awf.node.stack_launcher`` Protocol bodies and helpers.

These tests exercise the two ``Protocol`` method bodies (the ``...`` statements
that the type-checking seams declare) and the pure timeout/metadata helpers,
without touching Docker, the database, the network, or sleeping.
"""

from __future__ import annotations

import pytest

from awf.node import stack_launcher
from awf.node.stack_launcher import (
    WorkspaceSecretLeaseResolver,
    WorkspaceStackLauncher,
    WorkspaceStackLaunchRequest,
    _companion_compose_up_timeout_seconds,
    _stack_secret_metadata,
    effective_compose_up_timeout_seconds,
)

pytestmark = pytest.mark.unit


class _ConcreteStackLauncher(WorkspaceStackLauncher):
    """Concrete subclass whose ``launch`` defers to the Protocol body.

    Calling ``super().launch(...)`` executes the ``...`` statement in the
    ``WorkspaceStackLauncher`` Protocol (stack_launcher.py line 56), which
    evaluates to ``None``.
    """

    async def launch(self, request: WorkspaceStackLaunchRequest):  # type: ignore[override]
        return await super().launch(request)


class _ConcreteSecretLeaseResolver(WorkspaceSecretLeaseResolver):
    """Concrete subclass whose ``resolve`` defers to the Protocol body.

    Calling ``super().resolve(...)`` executes the ``...`` statement in the
    ``WorkspaceSecretLeaseResolver`` Protocol (stack_launcher.py line 69),
    which evaluates to ``None``.
    """

    def resolve(self, profile, *, workspace_id):  # type: ignore[override]
        return super().resolve(profile, workspace_id=workspace_id)


async def test_stack_launcher_protocol_body_returns_none() -> None:
    """The ``WorkspaceStackLauncher.launch`` Protocol body (line 56) runs."""
    launcher = _ConcreteStackLauncher()
    request = WorkspaceStackLaunchRequest(
        workspace_id="ws-protocol",
        layout=object(),  # type: ignore[arg-type]
        profile=object(),  # type: ignore[arg-type]
    )

    result = await launcher.launch(request)

    assert result is None


def test_secret_lease_resolver_protocol_body_returns_none() -> None:
    """The ``WorkspaceSecretLeaseResolver.resolve`` Protocol body (line 69) runs."""
    resolver = _ConcreteSecretLeaseResolver()

    result = resolver.resolve(object(), workspace_id="ws-protocol")

    assert result is None


class _DockerConfig:
    def __init__(self, startup_timeout_seconds: int) -> None:
        self.startup_timeout_seconds = startup_timeout_seconds


class _Profile:
    def __init__(self, startup_timeout_seconds: int) -> None:
        self.docker = _DockerConfig(startup_timeout_seconds)


class _SpecLikeCompanion:
    """A non-materialized companion exposing ``compose_up_timeout_seconds``.

    Stands in for ``WorkspaceCompanionSpec`` so the ``else`` branch of
    ``_companion_compose_up_timeout_seconds`` is exercised without building the
    full materialized dataclass.
    """

    def __init__(self, compose_up_timeout_seconds: int | None) -> None:
        self.compose_up_timeout_seconds = compose_up_timeout_seconds


def test_companion_timeout_reads_spec_like_value() -> None:
    """Non-materialized companions report their own ``compose_up_timeout_seconds``."""
    companion = _SpecLikeCompanion(compose_up_timeout_seconds=42)

    assert _companion_compose_up_timeout_seconds(companion) == 42  # type: ignore[arg-type]


def test_companion_timeout_none_is_passed_through() -> None:
    """A ``None`` companion timeout is returned unchanged (filtered downstream)."""
    companion = _SpecLikeCompanion(compose_up_timeout_seconds=None)

    assert _companion_compose_up_timeout_seconds(companion) is None  # type: ignore[arg-type]


def test_effective_timeout_takes_max_over_profile_and_companions() -> None:
    """The effective timeout is the max of the profile and companion budgets."""
    profile = _Profile(startup_timeout_seconds=30)
    companions = (
        _SpecLikeCompanion(compose_up_timeout_seconds=120),
        _SpecLikeCompanion(compose_up_timeout_seconds=None),
        _SpecLikeCompanion(compose_up_timeout_seconds=90),
    )

    result = effective_compose_up_timeout_seconds(
        profile=profile,  # type: ignore[arg-type]
        companions=companions,  # type: ignore[arg-type]
    )

    assert result == 120


def test_effective_timeout_falls_back_to_profile_when_no_companions() -> None:
    """With no companion overrides the profile startup timeout wins."""
    profile = _Profile(startup_timeout_seconds=300)

    result = effective_compose_up_timeout_seconds(
        profile=profile,  # type: ignore[arg-type]
        companions=(),
    )

    assert result == 300


def test_stack_secret_metadata_without_lease_uses_companion_only() -> None:
    """With no lease resolution only companion env metadata is merged."""
    metadata = _stack_secret_metadata(
        secret_lease_resolution=None,
        companion_secret_metadata={"COMPANION_TOKEN": {"mount": "/run/x"}},
    )

    assert metadata == {"COMPANION_TOKEN": {"mount": "/run/x"}}


class _LeaseResolution:
    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata


def test_stack_secret_metadata_merges_lease_and_companion() -> None:
    """Companion metadata overrides lease metadata for colliding keys on merge."""
    lease = _LeaseResolution(metadata={"SHARED": "lease", "LEASE_ONLY": 1})

    metadata = _stack_secret_metadata(
        secret_lease_resolution=lease,  # type: ignore[arg-type]
        companion_secret_metadata={"SHARED": "companion", "COMPANION_ONLY": 2},
    )

    assert metadata == {
        "SHARED": "companion",
        "LEASE_ONLY": 1,
        "COMPANION_ONLY": 2,
    }


def test_module_exposes_execution_error() -> None:
    """The service execution error remains a public ``Exception`` subclass."""
    assert issubclass(stack_launcher.WorkspaceServiceExecutionError, Exception)
