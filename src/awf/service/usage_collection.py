"""ccusage-backed implementation of the ``UsageSampler`` protocol.

``CcusageCollector`` samples per-run LLM usage from inside a workspace's agent
container by running a pinned ``ccusage`` with ``--json --offline``. It is wired
into ``AgentAdapter.run`` (the single shared agent chokepoint) so both normal
workspace execution and PR-monitor/recovery runs are covered without duplicating
provider-specific runner code.

Design invariants:
- Sampling never masks the agent outcome (all sample failures are reason-coded
  or swallowed-and-logged).
- Reported totals are baseline-subtracted so copied host history can't inflate
  them; a fresh baseline is captured at the start of each run (never reused from
  a prior run, whose transcripts persist in the per-workspace auth copy) so
  prior-run usage can't inflate this run's per-run total.
- Only normalized numeric/accounting data is persisted (see ``usage_store``).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from awf.adapters.usage import UsageSampleContext, UsageSampler
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncStreamingCommandRunner,
    CommandResult,
)
from awf.common.compose_exec import build_tracked_compose_exec
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime
from awf.service.usage_store import (
    REASON_COMMAND_FAILED,
    REASON_NO_RECORDS,
    REASON_SOURCE_UNSUPPORTED,
    REASON_TIMEOUT,
    REASON_UNAVAILABLE,
    NormalizedUsage,
    UsageSnapshot,
    normalize_ccusage_json,
    provider_ccusage_source,
    subtract_baseline,
    write_usage_snapshot,
)

_log = get_logger(__name__)

DEFAULT_SAMPLE_INTERVAL_SECONDS = 60.0
DEFAULT_CCUSAGE_COMMAND_TIMEOUT_SECONDS = 20.0


class _Clock(Protocol):
    def now(self) -> datetime: ...  # pragma: no cover - Protocol declaration only.

    async def sleep(self, seconds: float) -> None: ...  # pragma: no cover - Protocol decl.


class _RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _is_missing_binary(result: CommandResult) -> bool:
    if result.returncode == 127:
        return True
    # A real missing binary exits 127 (handled above); only treat stderr as a
    # missing-binary signal for the exact phrases shells emit ("command not
    # found" from bash, "no such file" from a failed exec/setsid). This keeps
    # app-level errors that merely contain "not found" (e.g. ccusage
    # "source not found") classified as REASON_COMMAND_FAILED.
    stderr_lower = result.stderr.lower()
    return "command not found" in stderr_lower or "no such file" in stderr_lower


class CcusageCollector(UsageSampler):
    """Samples ccusage usage inside the agent container every ``interval`` seconds."""

    def __init__(
        self,
        *,
        runner: AsyncStreamingCommandRunner,
        work_dir: str | Path,
        clock: _Clock | None = None,
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        command_timeout_seconds: float = DEFAULT_CCUSAGE_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner
        self._work_dir = Path(work_dir)
        self._clock = clock or _RealClock()
        self._interval_seconds = interval_seconds
        self._command_timeout_seconds = command_timeout_seconds

    async def start(
        self,
        *,
        compose_project: str,
        compose_file: Path,
        workspace_id: str,
        provider: AgentRuntime,
    ) -> _CcusageSampleContext:
        source = provider_ccusage_source(provider)
        ctx = _CcusageSampleContext(
            collector=self,
            compose_project=compose_project,
            compose_file=compose_file,
            workspace_id=workspace_id,
            provider=provider,
            source=source,
        )
        if source is None:
            # Unsupported provider: record the reason once, no periodic loop.
            await ctx._safe_sample(phase="live", run_status="running")
            return ctx
        await ctx._capture_baseline()
        ctx._task = asyncio.create_task(ctx._run_loop())
        return ctx


class _CcusageSampleContext(UsageSampleContext):
    """Per-run sampling handle returned by ``CcusageCollector.start``."""

    def __init__(
        self,
        *,
        collector: CcusageCollector,
        compose_project: str,
        compose_file: Path,
        workspace_id: str,
        provider: AgentRuntime,
        source: str | None,
    ) -> None:
        self._collector = collector
        self._compose_project = compose_project
        self._compose_file = compose_file
        self._workspace_id = workspace_id
        self._provider = provider
        self._source = source
        self._baseline: NormalizedUsage | None = None
        # Set when baseline capture failed (vs. a fresh, legitimately empty one).
        # While set, samples report unavailable instead of subtracting against a
        # missing baseline and leaking copied host history into the totals.
        self._baseline_unavailable_reason: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._finalized = False

    async def finalize(self, *, status: str) -> None:
        if self._finalized:
            return
        self._finalized = True
        final_task = asyncio.create_task(self._finalize_inner(status))
        # Shield the final sample so it still completes if the agent run is being
        # cancelled (the await below may be cancelled repeatedly).
        while not final_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(final_task)

    async def _finalize_inner(self, status: str) -> None:
        await self._cancel_loop()
        await self._safe_sample(phase="final", run_status=status)

    async def _cancel_loop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run_loop(self) -> None:
        while True:
            await self._collector._clock.sleep(self._collector._interval_seconds)
            await self._safe_sample(phase="live", run_status="running")

    async def _capture_baseline(self) -> None:
        # Always capture a fresh reading at run start; never reuse a prior
        # snapshot's baseline. The per-workspace auth copy persists across
        # retries and recovery runs (auth_mounts skips the copytree when the
        # target dir already exists, and the copy is only GC'd once the
        # workspace completes), so the previous run's transcripts are still on
        # disk when the next run starts. Reusing the old start-of-prior-run
        # baseline would let the prior run's tokens inflate this run's per-run
        # total; a fresh reading anchors the baseline at this run's true start.
        try:
            usage, reason, _model = await self._run_ccusage()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unexpected runner failure: swallow-and-log so the agent outcome is
            # never masked, but flag the baseline as unanchored so later samples
            # don't subtract against nothing and leak copied host history.
            _log.warning(
                "usage.collect.baseline_error",
                workspace_id=self._workspace_id,
                exc_info=True,
            )
            self._baseline_unavailable_reason = REASON_COMMAND_FAILED
            return
        if usage is not None:
            self._baseline = usage
            return
        if reason == REASON_NO_RECORDS:
            # Fresh workspace: ccusage ran cleanly with no prior host usage, so an
            # empty baseline is correct and later totals are genuine workspace usage.
            return
        # ccusage failed for a classified reason (timeout / command error /
        # unreadable output): we can't anchor a trustworthy baseline, so flag it.
        self._baseline_unavailable_reason = reason or REASON_COMMAND_FAILED

    async def _safe_sample(self, *, phase: str, run_status: str) -> None:
        try:
            await self._sample_and_write(phase=phase, run_status=run_status)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "usage.collect.error",
                workspace_id=self._workspace_id,
                phase=phase,
                status=run_status,
                exc_info=True,
            )

    async def _sample_and_write(self, *, phase: str, run_status: str) -> None:
        if self._source is None:
            await self._write(
                phase=phase,
                run_status=run_status,
                status_label="unavailable",
                reason=REASON_SOURCE_UNSUPPORTED,
                metrics=NormalizedUsage(),
                model=None,
            )
            return
        usage, reason, model = await self._run_ccusage()
        if usage is None:
            await self._write(
                phase=phase,
                run_status=run_status,
                status_label="unavailable",
                reason=reason,
                metrics=NormalizedUsage(),
                model=None,
            )
            return
        if self._baseline_unavailable_reason is not None:
            # We have a current reading but never anchored a baseline, so a delta
            # would expose copied host history. Report unavailable with the
            # baseline failure reason instead of an inflated total.
            await self._write(
                phase=phase,
                run_status=run_status,
                status_label="unavailable",
                reason=self._baseline_unavailable_reason,
                metrics=NormalizedUsage(),
                model=None,
            )
            return
        delta = subtract_baseline(usage, self._baseline)
        await self._write(
            phase=phase,
            run_status=run_status,
            status_label="available",
            reason=None,
            metrics=delta,
            model=model,
        )

    async def _write(
        self,
        *,
        phase: str,
        run_status: str,
        status_label: str,
        reason: str | None,
        metrics: NormalizedUsage,
        model: str | None,
    ) -> None:
        snapshot = UsageSnapshot(
            workspace_id=self._workspace_id,
            provider=self._provider.value,
            ccusage_source=self._source,
            status=status_label,
            run_status=run_status,
            phase=phase,
            captured_at=self._collector._clock.now().isoformat(),
            reason=reason,
            model=model,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            total_tokens=metrics.total_tokens,
            cost_estimate=metrics.cost_estimate,
            currency=metrics.currency,
            baseline=self._baseline.as_baseline_dict() if self._baseline is not None else None,
        )
        # Offload the blocking mkdir/write/replace off the event loop; finalize()
        # shields its sample so the final write still completes under cancellation.
        await asyncio.to_thread(write_usage_snapshot, snapshot, work_dir=self._collector._work_dir)

    async def _run_ccusage(self) -> tuple[NormalizedUsage | None, str | None, str | None]:
        # Expected ccusage CLI contract (pinned at 20.0.3 in
        # docker/agent-runtime.Dockerfile): ``ccusage <source> daily --json --offline``,
        # where ``<source>`` is a positional provider sub-command ("claude" /
        # "codex" / "gemini" / "opencode"; see ``provider_ccusage_source``). A future
        # pin that moves the provider behind a flag (e.g. ``--source``) would make
        # this positional invocation degrade to REASON_COMMAND_FAILED, so re-verify
        # this argument order whenever the Dockerfile pin is bumped.
        invocation = build_tracked_compose_exec(
            compose_project=self._compose_project,
            compose_file=self._compose_file,
            cli_args=["ccusage", str(self._source), "daily", "--json", "--offline"],
            source="usage",
            label="ccusage",
        )
        result = await self._collector._runner.run_streaming(
            invocation.args,
            wall_timeout_seconds=self._collector._command_timeout_seconds,
            idle_timeout_seconds=self._collector._command_timeout_seconds,
        )
        if result.reason_code in (COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON):
            return None, REASON_TIMEOUT, None
        if not result.ok:
            failure_reason = (
                REASON_UNAVAILABLE if _is_missing_binary(result) else REASON_COMMAND_FAILED
            )
            return None, failure_reason, None
        usage, reason = normalize_ccusage_json(result.stdout)
        model = usage.model if usage is not None else None
        return usage, reason, model
