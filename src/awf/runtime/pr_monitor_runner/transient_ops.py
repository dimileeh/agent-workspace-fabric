"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import (
    GitHubClientError,
    RepoRef,
)
from awf.db.repositories import WorkspaceEventCreate
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.constants import (
    _BITBUCKET_TRANSIENT_RETRY_EXHAUSTED_REASON,
    _BITBUCKET_TRANSIENT_RETRY_REASON,
    _GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON,
    _GIT_BASE_FETCH_TRANSIENT_RETRY_REASON,
    _GIT_FETCH_BASE_FAILED_REASON,
    _GITHUB_TRANSIENT_RETRY_EXHAUSTED_REASON,
    _GITHUB_TRANSIENT_RETRY_REASON,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _base_fetch_retry_wait_seconds,
    _clear_transient_forge_retry_state,
    _collect_defer_items,
    _exponential_backoff_wait_seconds,
    _increment_base_fetch_retry_count,
    _increment_forge_transient_retry_count,
    _is_transient_base_fetch_error,
    _is_transient_bitbucket_client_error,
    _is_transient_github_client_error,
    _transient_base_fetch_retry_payload,
    _transient_bitbucket_retry_payload,
    _transient_github_retry_payload,
    _with_ci_failures,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    _BaseFetchHandlingResult,
)


async def _fetch_status_for_decision(
    self: Any,
    *,
    repo: RepoRef,
    pr_number: int,
    workspace_id: str,
    base_branch: str,
) -> PRStatus:
    """Fetch the full PR snapshot used by the decision core.

    Includes the local base-behind calculation and, for failing CI,
    per-check logs. The same path is used for the main loop and the
    pre-merge recheck so the final merge gate cannot accidentally use
    weaker data than ordinary polling.
    """
    worktree_path = self._worktrees_root / workspace_id
    await self._fetch_base(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        base_branch=base_branch,
    )
    base_behind = await self._count_base_behind(
        worktree_path=worktree_path,
        base_branch=base_branch,
    )
    status = await self._deps.gh.fetch_pr_status(
        repo=repo, pr_number=pr_number, base_behind_count=base_behind
    )
    if status.check_state.value == "FAILURE":
        pytest_fallback_commands = await self._workspace_test_commands(workspace_id)
        failures = await self._deps.gh.fetch_failing_check_logs(
            repo=repo,
            pr_number=pr_number,
            head_sha=status.head_sha,
            pytest_fallback_commands=pytest_fallback_commands,
        )
        status = _with_ci_failures(status, failures)
    return cast(PRStatus, status)


@dataclass(frozen=True)
class _ForgeTransientRetrySpec:
    """Forge-specific knobs for the shared bounded transient-retry routine.

    The control flow for waiting on a recoverable GitHub vs Bitbucket blip is
    byte-identical; only the classifier, payload builder, structured-event names,
    and reason codes differ per forge. Capturing exactly those differences here
    lets :func:`_run_bounded_transient_forge_retry` carry the single shared
    implementation while each forge keeps emitting its own distinct event +
    reason code (behaviour-preserving).
    """

    is_transient: Callable[[Any], bool]
    build_payload: Callable[..., dict[str, object]]
    retrying_event: str
    exhausted_event: str
    retrying_reason: str
    exhausted_reason: str


_GITHUB_TRANSIENT_RETRY_SPEC = _ForgeTransientRetrySpec(
    is_transient=_is_transient_github_client_error,
    build_payload=_transient_github_retry_payload,
    retrying_event="monitor.github_transient_error_retrying",
    exhausted_event="monitor.github_transient_error_retry_exhausted",
    retrying_reason=_GITHUB_TRANSIENT_RETRY_REASON,
    exhausted_reason=_GITHUB_TRANSIENT_RETRY_EXHAUSTED_REASON,
)


_BITBUCKET_TRANSIENT_RETRY_SPEC = _ForgeTransientRetrySpec(
    is_transient=_is_transient_bitbucket_client_error,
    build_payload=_transient_bitbucket_retry_payload,
    retrying_event="monitor.bitbucket_transient_error_retrying",
    exhausted_event="monitor.bitbucket_transient_error_retry_exhausted",
    retrying_reason=_BITBUCKET_TRANSIENT_RETRY_REASON,
    exhausted_reason=_BITBUCKET_TRANSIENT_RETRY_EXHAUSTED_REASON,
)


async def _run_bounded_transient_forge_retry(
    self: Any,
    exc: ForgeClientError,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
    spec: _ForgeTransientRetrySpec,
) -> bool:
    """Shared bounded-backoff retry routine for one forge's transient blips.

    A transient classification (per ``spec.is_transient``) increments a
    per-context retry count and, while under the budget, sleeps an exponential
    backoff and returns ``True`` (retry). Once the budget is exhausted it emits a
    distinct exhausted event, persists state, and returns ``False`` so the caller
    terminates via its existing path with the forge's preserved ``reason_code``. A
    non-transient (deterministic) fault returns ``False`` immediately. Mirrors the
    base-fetch bounded-retry machinery with its own config + per-context counter so
    the two never entangle; ``spec`` supplies the only forge-specific differences
    (classifier, payload builder, event names, reason codes).
    """
    if not spec.is_transient(exc):
        return False
    retry_number = _increment_forge_transient_retry_count(state, context)
    max_retries = max(self._runner_config.transient_forge_max_retries, 0)
    if retry_number > max_retries:
        payload = spec.build_payload(
            exc,
            context=context,
            pr_number=pr_number,
            wait_seconds=0.0,
            retry_number=retry_number,
            max_retries=max_retries,
        )
        _log.warning(
            spec.exhausted_event,
            workspace_id=workspace_id,
            **payload,
        )
        await self._write_monitor_log(
            monitor_log,
            {
                "event": spec.exhausted_event,
                "workspace_id": workspace_id,
                "reason_code": spec.exhausted_reason,
                **payload,
            },
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type=spec.exhausted_event,
                    reason_code=spec.exhausted_reason,
                    payload=payload,
                )
            ],
        )
        # Persist only the bounded retry counter, never the whole in-memory state:
        # a fix-cycle caller may still hold an unconfirmed addressed verdict that it
        # rolls back only after this returns, and flushing it here would let a
        # crash/restart reload it as addressed and merge over still-open feedback
        # (#305). See _persist_forge_transient_retry_count.
        await self._persist_forge_transient_retry_count(
            workspace_id, context=context, retry_number=retry_number
        )
        return False
    wait_seconds = _exponential_backoff_wait_seconds(
        retry_number=retry_number,
        initial_backoff_seconds=self._runner_config.transient_forge_initial_backoff_seconds,
        max_backoff_seconds=self._runner_config.transient_forge_max_backoff_seconds,
    )
    payload = spec.build_payload(
        exc,
        context=context,
        pr_number=pr_number,
        wait_seconds=wait_seconds,
        retry_number=retry_number,
        max_retries=max_retries,
    )
    _log.warning(
        spec.retrying_event,
        workspace_id=workspace_id,
        **payload,
    )
    await self._write_monitor_log(
        monitor_log,
        {
            "event": spec.retrying_event,
            "workspace_id": workspace_id,
            "reason_code": spec.retrying_reason,
            **payload,
        },
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type=spec.retrying_event,
                reason_code=spec.retrying_reason,
                payload=payload,
            )
        ],
    )
    # Persist only the bounded retry counter, never the whole in-memory state: a
    # fix-cycle caller may still hold an unconfirmed addressed verdict that it rolls
    # back only after this returns, and flushing it here would let a crash/restart
    # during the backoff reload it as addressed and merge over still-open feedback
    # (#305). See _persist_forge_transient_retry_count.
    await self._persist_forge_transient_retry_count(
        workspace_id, context=context, retry_number=retry_number
    )
    await self._deps.sleep(wait_seconds)
    return True


async def _wait_after_transient_github_error(
    self: Any,
    exc: GitHubClientError,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
) -> bool:
    """Wait and keep polling on a recoverable GitHub blip, with bounded backoff.

    A transient classification (5xx, rate-limit, transport, or an ambiguous
    ``HTTP 401`` / ``Requires authentication`` with no strong permanent marker —
    #515) increments a per-context retry count and, while under the budget, sleeps
    an exponential backoff and returns ``True`` (retry). Once the budget is
    exhausted it emits a distinct exhausted event, persists state, and returns
    ``False`` so the caller terminates via its existing path with the forge's
    preserved ``reason_code``. A non-transient (strong permanent) fault returns
    ``False`` immediately. Thin wrapper over
    :func:`_run_bounded_transient_forge_retry` with the GitHub spec.
    """
    return await _run_bounded_transient_forge_retry(
        self,
        exc,
        workspace_id=workspace_id,
        pr_number=pr_number,
        context=context,
        state=state,
        monitor_log=monitor_log,
        spec=_GITHUB_TRANSIENT_RETRY_SPEC,
    )


async def _wait_after_transient_bitbucket_error(
    self: Any,
    exc: BitbucketClientError,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
) -> bool:
    """Wait and keep polling on a recoverable Bitbucket blip, with bounded backoff.

    Mirrors :func:`_wait_after_transient_github_error` so Bitbucket workspaces
    survive transient rate-limit/transport/5xx faults — and, symmetric with
    GitHub's ambiguous-401 handling (#515), any 401/403 auth blip — instead of
    terminating immediately. A transient classification increments a per-context
    retry count and, while under the budget, sleeps an exponential backoff and
    returns ``True`` (retry); once exhausted it emits a distinct exhausted event,
    persists state, and returns ``False`` so the caller terminates with the
    preserved ``reason_code``. A deterministic fault returns ``False`` immediately.
    Thin wrapper over :func:`_run_bounded_transient_forge_retry` with the Bitbucket
    spec.
    """
    return await _run_bounded_transient_forge_retry(
        self,
        exc,
        workspace_id=workspace_id,
        pr_number=pr_number,
        context=context,
        state=state,
        monitor_log=monitor_log,
        spec=_BITBUCKET_TRANSIENT_RETRY_SPEC,
    )


async def _wait_after_transient_forge_error(
    self: Any,
    exc: ForgeClientError,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
) -> bool:
    """Wait and keep polling on a recoverable forge (GitHub/Bitbucket) blip.

    The forge-neutral entry point for the consolidated ``except ForgeClientError``
    catch arms: it dispatches to the per-forge wait helper so each keeps emitting
    its own transient-retry event and reason code (behaviour-preserving), while
    callers no longer need to branch on the concrete forge type. ``state`` carries
    the per-context bounded-retry counter through to the dispatched helper. Returns
    ``True`` when the caller should retry, ``False`` when the fault is
    deterministic or the bounded retry budget is exhausted.
    """
    if isinstance(exc, BitbucketClientError):
        return await _wait_after_transient_bitbucket_error(
            self,
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context=context,
            state=state,
            monitor_log=monitor_log,
        )
    if isinstance(exc, GitHubClientError):
        return await _wait_after_transient_github_error(
            self,
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context=context,
            state=state,
            monitor_log=monitor_log,
        )
    # No per-forge wait helper exists for an unknown subclass, so we cannot emit
    # a transient-retry event or honour a backoff. Fail loudly rather than
    # silently classifying a possibly-transient fault as deterministic: a new
    # forge must extend this dispatch. Mirrors the exhaustiveness raise in
    # ``loop.py``.
    raise RuntimeError(  # pragma: no cover - only GitHub/Bitbucket subclasses exist
        f"unknown ForgeClientError subclass: {type(exc).__name__}"
    )


async def _clear_forge_transient_retry_state_on_success(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    context: str,
) -> None:
    """Reset a context's bounded forge-transient retry counter after success.

    The per-context counter (:func:`_increment_forge_transient_retry_count`) is
    persisted on every transient forge blip so a *consecutive* run of failures
    stays bounded across polls. Once that context's forge operation finally
    succeeds the incident is over, so the counter must be cleared — otherwise a
    recovered one-off blip leaves a stale count behind, and later unrelated blips
    in the same context resume from it and exhaust the bounded budget
    prematurely, downgrading or terminating an otherwise-healthy monitor.

    Mirrors the inline clear the ``fetch_pr_status`` / ``pre_merge_recheck`` paths
    already perform, factored into one helper so every increment context resets
    symmetrically. Persists only when a counter was actually present, so the
    common no-blip success path adds no extra DB write.

    The persist removes *only* the counter key, never the whole in-memory state
    (:func:`_remove_forge_transient_retry_count`): a fix-cycle caller can hold
    unconfirmed addressed verdicts for *later* ``threads_to_resolve`` whose forge
    resolve calls have not run yet, and flushing those here would let a cancel
    during a subsequent transient resolve wait reload them as addressed so
    ``decide()`` skips still-open feedback and merges over it (#305) — symmetric
    with :func:`_persist_forge_transient_retry_count` on the wait path.
    """
    if _clear_transient_forge_retry_state(state, context=context):
        await self._remove_forge_transient_retry_count(workspace_id, context=context)


async def _wait_after_transient_base_fetch_error(
    self: Any,
    exc: BaseFetchError,
    *,
    workspace_id: str,
    pr_number: int,
    context: str,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
) -> _BaseFetchHandlingResult:
    if not _is_transient_base_fetch_error(exc):
        return _BaseFetchHandlingResult(
            retry=False,
            reason_code=_GIT_FETCH_BASE_FAILED_REASON,
        )

    retry_number = _increment_base_fetch_retry_count(state, context)
    max_retries = max(self._runner_config.transient_base_fetch_max_retries, 0)
    if retry_number > max_retries:
        payload = _transient_base_fetch_retry_payload(
            exc,
            context=context,
            pr_number=pr_number,
            retry_number=retry_number,
            max_retries=max_retries,
            wait_seconds=0.0,
        )
        _log.warning(
            "monitor.git_base_fetch_transient_retry_exhausted",
            workspace_id=workspace_id,
            **payload,
        )
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.git_base_fetch_transient_retry_exhausted",
                "workspace_id": workspace_id,
                "reason_code": _GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON,
                **payload,
            },
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="monitor.git_base_fetch_transient_retry_exhausted",
                    reason_code=_GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON,
                    payload=payload,
                )
            ],
        )
        await self._persist_state(workspace_id, state)
        return _BaseFetchHandlingResult(
            retry=False,
            reason_code=_GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON,
        )

    wait_seconds = _base_fetch_retry_wait_seconds(
        retry_number=retry_number,
        initial_backoff_seconds=(self._runner_config.transient_base_fetch_initial_backoff_seconds),
        max_backoff_seconds=self._runner_config.transient_base_fetch_max_backoff_seconds,
    )
    payload = _transient_base_fetch_retry_payload(
        exc,
        context=context,
        pr_number=pr_number,
        retry_number=retry_number,
        max_retries=max_retries,
        wait_seconds=wait_seconds,
    )
    _log.warning(
        "monitor.git_base_fetch_transient_retrying",
        workspace_id=workspace_id,
        **payload,
    )
    await self._write_monitor_log(
        monitor_log,
        {
            "event": "monitor.git_base_fetch_transient_retrying",
            "workspace_id": workspace_id,
            "reason_code": _GIT_BASE_FETCH_TRANSIENT_RETRY_REASON,
            **payload,
        },
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="monitor.git_base_fetch_transient_retrying",
                reason_code=_GIT_BASE_FETCH_TRANSIENT_RETRY_REASON,
                payload=payload,
            )
        ],
    )
    await self._persist_state(workspace_id, state)
    await self._deps.sleep(wait_seconds)
    return _BaseFetchHandlingResult(
        retry=True,
        reason_code=_GIT_BASE_FETCH_TRANSIENT_RETRY_REASON,
    )


def _write_defer_signal(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    terminal_action: str,
    merged: bool,
    status: PRStatus,
    state: MonitorState,
) -> None:
    """Persist a machine-readable drop of the workspace's terminal
    state for an orchestrator to consume.

    The file always exists when the runner reaches a terminal
    action — empty ``deferred_*_items`` lists when nothing was
    deferred. Downstream tooling can therefore poll for the file's
    presence as the authoritative "monitor is done" signal without
    also having to handle a missing-file case.

    Called with a best-effort contract: a failure to write MUST NOT
    stop the state-machine transition (the DB write has priority).
    """
    try:
        self._artifacts_root.mkdir(parents=True, exist_ok=True)
        bot_items, human_items = _collect_defer_items(status, state)
        payload = {
            "workspace_id": workspace_id,
            "pr_number": pr_number,
            "terminal_action": terminal_action,
            "merged": merged,
            "deferred_bot_items": bot_items,
            "deferred_human_items": human_items,
        }
        out_path = self._artifacts_root / f"{workspace_id}.defer-signal.json"
        # Atomic publish: write to a sibling temp file, then rename.
        # Pollers treat presence of out_path as the terminal signal, so
        # they must never observe a partially-written JSON payload.
        tmp_path = out_path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(out_path)
    except Exception as exc:
        _log.warning(
            "monitor.defer_signal_write_failed",
            workspace_id=workspace_id,
            error=repr(exc)[:400],
        )
