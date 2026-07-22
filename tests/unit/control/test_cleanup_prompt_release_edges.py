"""Prompt terminal-runtime release edge coverage."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.worker import cleanup as worker_cleanup
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus


def _candidate(workspace_id: str = "ws1") -> _TerminalRuntimeCandidate:
    return _TerminalRuntimeCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.failed,
        repo_url="https://example.test/repo.git",
        compose_project_name=f"awf_{workspace_id}",
        compose_file_path=f"/tmp/{workspace_id}/compose.yml",
    )


class _RecordingLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))


# These exercise the control flow of ``_release_terminal_runtime_promptly`` over
# its mockable seams (``_load_terminal_runtime_candidate`` /
# ``_release_terminal_runtime_for_candidate`` / ``_runtime_cleaner``). The
# DB-backed candidate loading and the end-to-end teardown/idempotency are covered
# by the real-Postgres tests in ``test_worker_parts/test_worker_part_051.py``.


@pytest.mark.unit
async def test_prompt_release_noop_when_no_runtime_cleaner() -> None:
    """With no runtime cleaner wired the prompt release is a clean no-op: it must
    not even load a candidate, so adding the hook to a cleaner-less worker is safe."""
    load_calls: list[str] = []

    async def _load(workspace_id: str) -> _TerminalRuntimeCandidate | None:
        load_calls.append(workspace_id)
        return _candidate(workspace_id)

    worker = SimpleNamespace(
        _runtime_cleaner=None,
        _load_terminal_runtime_candidate=_load,
        _release_terminal_runtime_for_candidate=None,
    )

    await worker_cleanup._release_terminal_runtime_promptly(worker, "ws1")  # noqa: SLF001

    assert load_calls == []


@pytest.mark.unit
async def test_prompt_release_noop_when_candidate_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the workspace is not a release candidate (non-terminal / missing /
    foreign-node), the per-candidate teardown is never invoked and nothing logs."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    release_calls: list[_TerminalRuntimeCandidate] = []

    async def _load(workspace_id: str) -> _TerminalRuntimeCandidate | None:
        del workspace_id
        return None

    async def _release(candidate: _TerminalRuntimeCandidate) -> None:
        release_calls.append(candidate)

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _load_terminal_runtime_candidate=_load,
        _release_terminal_runtime_for_candidate=_release,
    )

    await worker_cleanup._release_terminal_runtime_promptly(worker, "ws1")  # noqa: SLF001

    assert release_calls == []
    assert log.warnings == []


@pytest.mark.unit
async def test_prompt_release_delegates_to_existing_per_candidate_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt path reuses the existing per-candidate teardown (DRY) for a
    loaded terminal candidate, and does not log on success."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_prompt")
    release_calls: list[_TerminalRuntimeCandidate] = []

    async def _load(workspace_id: str) -> _TerminalRuntimeCandidate | None:
        assert workspace_id == "ws_prompt"
        return candidate

    async def _release(loaded: _TerminalRuntimeCandidate) -> None:
        release_calls.append(loaded)

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _load_terminal_runtime_candidate=_load,
        _release_terminal_runtime_for_candidate=_release,
    )

    await worker_cleanup._release_terminal_runtime_promptly(worker, "ws_prompt")  # noqa: SLF001

    assert release_calls == [candidate]
    assert log.warnings == []


@pytest.mark.unit
async def test_prompt_release_failure_does_not_break_terminal_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt-release failure is swallowed and logged with its dedicated reason
    code; it must never propagate, so the terminal transition still completes and
    the interval backstop reclaims the miss later."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_fail")

    async def _load(workspace_id: str) -> _TerminalRuntimeCandidate | None:
        del workspace_id
        return candidate

    async def _release(_loaded: _TerminalRuntimeCandidate) -> None:
        raise RuntimeError("compose down blew up")

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _load_terminal_runtime_candidate=_load,
        _release_terminal_runtime_for_candidate=_release,
    )

    # Must not raise: the terminal transition is never broken by a release failure.
    await worker_cleanup._release_terminal_runtime_promptly(worker, "ws_fail")  # noqa: SLF001

    assert [event for event, _ in log.warnings] == [
        "worker.prompt_terminal_runtime_release_failed",
    ]
    _, fields = log.warnings[0]
    assert fields["workspace_id"] == "ws_fail"
    assert fields["reason_code"] == "PROMPT_TERMINAL_RUNTIME_RELEASE_FAILED"
    assert fields["error_type"] == "RuntimeError"
    assert "compose down blew up" in fields["error"]


@pytest.mark.unit
async def test_prompt_release_completes_across_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external cancel (worker shutdown) landing mid-teardown must not tear the
    ``compose down`` apart and re-leak the stack: the shielded teardown runs to
    completion, then the CancelledError still propagates (mirrors
    ``_release_execution_claim_after_cancellation``'s cancellation contract)."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_cancel")
    started = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []

    async def _load(workspace_id: str) -> _TerminalRuntimeCandidate | None:
        del workspace_id
        return candidate

    async def _release(loaded: _TerminalRuntimeCandidate) -> None:
        started.set()
        await release.wait()
        completed.append(loaded.workspace_id)

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _load_terminal_runtime_candidate=_load,
        _release_terminal_runtime_for_candidate=_release,
    )

    task = asyncio.create_task(
        worker_cleanup._release_terminal_runtime_promptly(worker, "ws_cancel")  # noqa: SLF001
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    # Let the cancel be delivered to (and suppressed by) the shield-and-reawait.
    await asyncio.sleep(0)
    # The shielded teardown has not been torn apart: it is still mid-flight.
    assert completed == []

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The teardown still ran to completion despite the cancellation.
    assert completed == ["ws_cancel"]
    assert log.warnings == []


@pytest.mark.unit
async def test_prompt_release_swallows_internal_self_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the teardown coroutine self-cancels internally (a ``CancelledError``
    propagated out of ``_release_terminal_runtime_for_candidate``'s re-raise
    guards), ``body_task`` is marked *cancelled* so ``body_task.exception()``
    would itself raise. The ``cancelled()`` guard treats it as the swallow-and-log
    path (no spurious warning), and the cancel is re-raised once via the
    shield-and-reawait ``observed_cancel`` re-raise, never shadowing it with an
    ``.exception()`` raise."""
    log = _RecordingLog()
    monkeypatch.setattr(worker_cleanup, "_log", log)
    candidate = _candidate("ws_self_cancel")

    async def _load(workspace_id: str) -> _TerminalRuntimeCandidate | None:
        del workspace_id
        return candidate

    async def _release(_loaded: _TerminalRuntimeCandidate) -> None:
        raise asyncio.CancelledError

    worker = SimpleNamespace(
        _runtime_cleaner=object(),
        _load_terminal_runtime_candidate=_load,
        _release_terminal_runtime_for_candidate=_release,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker_cleanup._release_terminal_runtime_promptly(  # noqa: SLF001
            worker, "ws_self_cancel"
        )

    # The self-cancel is not double-reported as a failure warning.
    assert log.warnings == []
