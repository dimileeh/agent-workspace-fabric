"""Precheck failures after a park keep its unsettled dependencies pending (#936).

A required-secret or companion-spec precheck fails before the restart runs, so
neither its failure event nor a park it causes may consume the dependencies the
guided retry after the earlier park must force-recreate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff
from awf.control.executor.constants import MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
from tests.unit.control.test_executor_monitor_recovery_parts.test_monitor_resume_restart_escalation import (  # noqa: E501
    _POSTGRES_COMPOSE,
    _dependency_health,
    _executor,
    _guide,
    _runtime_unusable,
    _ScriptedCompose,
    _seed,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (_runtime_unusable, factory, fake)


def _fail_the_precheck(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing_secret(**_kwargs: Any) -> None:
        raise monitor_handoff.CompanionEnvSecretPrecheckError(
            stderr="COMPANION_ENV_SECRET_SOURCE_MISSING: companion=api, target=TOKEN",
            reason_code="COMPANION_ENV_SECRET_SOURCE_MISSING",
        )

    monkeypatch.setattr(
        monitor_handoff, "_precheck_required_companion_env_secrets_for_resume", _missing_secret
    )


@pytest.mark.unit
@pytest.mark.parametrize("precheck_failures", [1, MONITOR_RUNTIME_RESTART_FAILURE_LIMIT])
async def test_precheck_failures_after_exhaustion_keep_the_unsettled_dependencies_pending(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    precheck_failures: int,
) -> None:
    """A precheck failure never reaches the restart, so it cannot consume them."""
    (tmp_path / "compose.yml").write_text(_POSTGRES_COMPOSE, encoding="utf-8")
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    compose.fail = False
    executor = _executor(fake, factory, tmp_path, compose)
    _dependency_health(monkeypatch, {"postgres": "unhealthy"})
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    await _guide(factory, ws_id)
    with monkeypatch.context() as precheck:
        _fail_the_precheck(precheck)
        for _ in range(precheck_failures):
            assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert compose.calls == []
    if precheck_failures == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT:
        # The precheck streak parked the workspace again.
        await _guide(factory, ws_id)

    _dependency_health(monkeypatch, {"postgres": "healthy"})
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert [(call.get("services"), call.get("force_recreate")) for call in compose.calls] == [
        (("postgres",), True),
        (("postgres",), None),
        (("agent",), True),
    ]
    compose.calls.clear()
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert [call.get("services") for call in compose.calls] == [("postgres",), ("agent",)]
