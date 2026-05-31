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
from awf.common.compose_exec import (
    TrackedComposeExec,
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
)
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

# Gap *between* samples, not a fixed wall-clock period: each loop iteration
# sleeps this long and then runs ccusage (up to the command timeout below), so
# the effective cadence is this value + sample time (~80s worst case at the
# defaults). See ``_run_loop`` for the full rationale; callers/tests overriding
# the interval should reason about sample frequency with that composition in mind.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 60.0
DEFAULT_CCUSAGE_COMMAND_TIMEOUT_SECONDS = 20.0

# Neutral ccusage config baked into the agent-runtime image (see
# docker/agent-runtime.Dockerfile). Passed via ``--config`` on every ccusage
# invocation so the collector loads ONLY this empty config and ccusage skips
# auto-discovery of ``.ccusage/ccusage.json`` (cwd), ``~/.claude/ccusage.json``
# and ``~/.config/claude/ccusage.json``. The per-workspace auth copy seeds
# ``~/.claude`` from the host (see ``_prepare_isolated_claude_auth``) and does
# not strip a host ``ccusage.json``, so without this pin a host config's
# ``since``/``until``/``project``/``breakdown``/``instances`` defaults (or
# per-``daily`` overrides) would silently filter the sampler's reading to partial
# totals or REASON_NO_RECORDS instead of full per-run usage. Pinning any path
# bypasses discovery even when the file is absent (ccusage then applies no
# config), so the isolation holds regardless; the baked empty file additionally
# guards against a future pin that might reject a missing ``--config`` target.
_CCUSAGE_NEUTRAL_CONFIG_PATH = "/opt/awf/ccusage-neutral.json"


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
    # A real missing binary exits 127 (handled above): on this Debian image both
    # the setsid (util-linux EXIT_NOTFOUND) and dash exec paths return 127 on
    # ENOENT. Only treat stderr as a secondary missing-binary signal for the
    # exact phrase shells emit ("command not found" from bash/sh). Avoid
    # "no such file" here: ccusage (Node) emits ENOENT errors for missing
    # config/data files (e.g. the --config neutral file) with that phrase in
    # stderr, which would misclassify a present-but-failing binary as
    # REASON_UNAVAILABLE ("ccusage not installed") instead of REASON_COMMAND_FAILED.
    return "command not found" in result.stderr.lower()


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
        # Most recent snapshot write, tracked so finalize() can drain an in-flight
        # live write before issuing its own final write (see _write / _drain_pending_write).
        self._pending_write: asyncio.Task[Path] | None = None

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
        await self._drain_pending_write()
        await self._safe_sample(phase="final", run_status=status)

    async def _cancel_loop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _drain_pending_write(self) -> None:
        # _cancel_loop only cancels the loop *task*; if it was awaiting a snapshot
        # write, the thread-pool job is uncancellable and keeps running to its
        # tmp.replace(target) rename. Wait for that write (kept reachable past the
        # cancellation by the shield in _write) to land before finalize issues its
        # own write, so a late live-write rename can't clobber the final snapshot
        # under the store's latest-wins semantics.
        pending = self._pending_write
        if pending is None:
            return
        with contextlib.suppress(Exception):
            await pending

    async def _run_loop(self) -> None:
        # ``interval_seconds`` (DEFAULT_SAMPLE_INTERVAL_SECONDS) is the sleep
        # *between* samples, not a fixed wall-clock period: each iteration then
        # runs _safe_sample, which can itself spend up to ``command_timeout_seconds``
        # waiting on ccusage, so the effective period is interval + sample time
        # (~80s worst case at the defaults). Sampling is a diagnostic, so this
        # gap-based cadence (no drift correction back to a fixed clock) is
        # intentional; tune the constant with that composition in mind.
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
            usage, reason, model = await self._run_ccusage()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unexpected runner failure: swallow-and-log so the agent outcome is
            # never masked, and flag the baseline as unanchored so later samples
            # don't subtract against nothing and leak copied host history.
            _log.warning(
                "usage.collect.baseline_error",
                workspace_id=self._workspace_id,
                exc_info=True,
            )
            self._baseline_unavailable_reason = REASON_COMMAND_FAILED
            # Still seed an immediate unavailable snapshot, like the classified-
            # failure path below. The reused workspace id means the prior run's
            # snapshot.json is the latest record on disk; skipping the write would
            # leave it reported as this run's usage until the first live tick (or
            # indefinitely if sampling keeps failing). Persist zeros + the baseline
            # failure reason — never the reading we failed to obtain — so usage
            # state stays consistent with this run without leaking host history.
            await self._safe_write_reading(
                usage=None,
                reason=REASON_COMMAND_FAILED,
                model=None,
                phase="live",
                run_status="running",
            )
            return
        if usage is not None:
            self._baseline = usage
        elif reason != REASON_NO_RECORDS:
            # ccusage failed for a classified reason (timeout / command error /
            # unreadable output): we can't anchor a trustworthy baseline, so flag
            # it. (REASON_NO_RECORDS is a fresh workspace: an empty baseline is
            # correct and later totals are genuine workspace usage.)
            self._baseline_unavailable_reason = reason or REASON_COMMAND_FAILED
        # Seed an immediate "running" snapshot from this same baseline reading.
        # The same workspace id is reused across retries/recovery, so the prior
        # run's snapshot.json is still the latest record on disk; without this
        # write it would stay latest until the first live sample (one interval
        # away) or finalization, and workspace_usage_summary would report the old
        # run's totals as this run's usage during that window. No extra ccusage
        # exec: the start reading is, by definition, a zero delta against the
        # baseline it just anchored (or an unavailable reason when it didn't).
        await self._safe_write_reading(
            usage=usage, reason=reason, model=model, phase="live", run_status="running"
        )

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

    async def _safe_write_reading(
        self,
        *,
        usage: NormalizedUsage | None,
        reason: str | None,
        model: str | None,
        phase: str,
        run_status: str,
    ) -> None:
        # Swallow-and-log wrapper for seeding the start snapshot from the baseline
        # reading. A failed seed write must not mask the agent outcome nor abort
        # the sampler (its caller runs on the agent start path), so it logs like
        # _safe_sample rather than propagating.
        try:
            await self._write_reading(
                usage=usage, reason=reason, model=model, phase=phase, run_status=run_status
            )
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
        await self._write_reading(
            usage=usage, reason=reason, model=model, phase=phase, run_status=run_status
        )

    async def _write_reading(
        self,
        *,
        usage: NormalizedUsage | None,
        reason: str | None,
        model: str | None,
        phase: str,
        run_status: str,
    ) -> None:
        # Persist one ccusage reading as a baseline-subtracted snapshot. Shared by
        # the periodic/final samples (reading fetched via _run_ccusage) and the
        # start-time seed snapshot (reading reused from _capture_baseline).
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
            cached_input_tokens=metrics.cached_input_tokens,
            output_tokens=metrics.output_tokens,
            reasoning_output_tokens=metrics.reasoning_output_tokens,
            total_tokens=metrics.total_tokens,
            cost_estimate=metrics.cost_estimate,
            currency=metrics.currency,
            baseline=self._baseline.as_baseline_dict() if self._baseline is not None else None,
        )
        # Offload the blocking mkdir/write/replace off the event loop, tracking the
        # write so finalize() can drain it. shield keeps the worker-thread job
        # reachable when a loop cancellation interrupts this await: the await raises
        # CancelledError but the thread keeps running to its tmp.replace(target)
        # rename, and the tracked task lets _drain_pending_write order that rename
        # before the final write. finalize() also shields its own sample so the
        # final write still completes under outer cancellation.
        self._pending_write = asyncio.create_task(
            asyncio.to_thread(write_usage_snapshot, snapshot, work_dir=self._collector._work_dir)
        )
        await asyncio.shield(self._pending_write)

    async def _run_ccusage(self) -> tuple[NormalizedUsage | None, str | None, str | None]:
        # Expected ccusage CLI contract (pinned at 20.0.3 in
        # docker/agent-runtime.Dockerfile): ``ccusage <source> daily --json --offline
        # --config <neutral>``, where ``<source>`` is a positional provider
        # sub-command ("claude" / "codex" / "gemini" / "opencode"; see
        # ``provider_ccusage_source``). Cursor is intentionally reported as
        # unsupported until the pinned ccusage release adds a cursor source.
        # ``--config`` pins a neutral config so
        # auto-discovered user/project ccusage configs can't filter per-run totals
        # (see ``_CCUSAGE_NEUTRAL_CONFIG_PATH``). A future pin that moves the provider
        # behind a flag (e.g. ``--source``) or renames/removes ``--config`` would make
        # this invocation degrade to REASON_COMMAND_FAILED, so re-verify the argument
        # order and flags whenever the Dockerfile pin is bumped. ``--offline`` is
        # ccusage's global ``-O`` option and is accepted by every source subcommand,
        # including ``opencode``, at the pinned 20.0.3 (verified: ``ccusage opencode
        # daily --help`` lists ``-O, --offline`` and the full invocation exits 0), so
        # it does not degrade opencode runs even though the docs page omits it.
        invocation = build_tracked_compose_exec(
            compose_project=self._compose_project,
            compose_file=self._compose_file,
            cli_args=[
                "ccusage",
                str(self._source),
                "daily",
                "--json",
                "--offline",
                "--config",
                _CCUSAGE_NEUTRAL_CONFIG_PATH,
            ],
            source="usage",
            label="ccusage",
        )
        result = await self._collector._runner.run_streaming(
            invocation.args,
            wall_timeout_seconds=self._collector._command_timeout_seconds,
            idle_timeout_seconds=self._collector._command_timeout_seconds,
        )
        if result.reason_code in (COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON):
            await self._cleanup_timed_out_invocation(invocation)
            return None, REASON_TIMEOUT, None
        if not result.ok:
            failure_reason = (
                REASON_UNAVAILABLE if _is_missing_binary(result) else REASON_COMMAND_FAILED
            )
            return None, failure_reason, None
        usage, reason = normalize_ccusage_json(result.stdout)
        model = usage.model if usage is not None else None
        return usage, reason, model

    async def _cleanup_timed_out_invocation(self, invocation: TrackedComposeExec) -> None:
        # On timeout, run_streaming kills the local compose client, but per the
        # build_tracked_compose_exec contract that does not prove the in-container
        # ccusage process tree is gone. Run targeted cleanup for this invocation so
        # orphaned ccusage processes can't accumulate across repeated timeouts and
        # degrade later samples (same pattern as AgentAdapter.run / validation). This
        # is best-effort diagnostics: swallow-and-log any cleanup failure (it already
        # logs an error internally) so the sample stays reason-coded as REASON_TIMEOUT
        # and the agent outcome is never masked.
        try:
            await cleanup_compose_exec_invocation(
                self._collector._runner,
                invocation,
                workspace_id=self._workspace_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "usage.collect.cleanup_error",
                workspace_id=self._workspace_id,
                invocation_id=invocation.invocation_id,
                exc_info=True,
            )
