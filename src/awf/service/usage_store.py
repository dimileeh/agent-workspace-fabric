"""AWF-owned durable store for normalized workspace LLM usage snapshots.

This module is intentionally dependency-light: it maps AWF agent runtimes to
``ccusage`` sources, normalizes ``ccusage --json`` output into safe numeric
accounting data, and reads/writes a single latest-wins snapshot per workspace
under ``<work_dir>/usage/<workspace_id>/snapshot.json``.

It never persists raw prompt JSONL, file paths, or credential-bearing content —
only normalized token/cost numbers plus safe metadata (provider, source, model,
status, reason codes, timestamps).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from awf.common.config import get_settings
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime

_log = get_logger(__name__)

SCHEMA_VERSION = 1
SNAPSHOT_FILENAME = "snapshot.json"
USAGE_SOURCE = "ccusage"

# Reason codes surfaced when ccusage usage is missing or invalid.
REASON_SOURCE_UNSUPPORTED = "ccusage_source_unsupported"
REASON_UNAVAILABLE = "ccusage_unavailable"
REASON_COMMAND_FAILED = "ccusage_command_failed"
REASON_TIMEOUT = "ccusage_timeout"
REASON_INVALID_JSON = "ccusage_invalid_json"
REASON_NO_RECORDS = "ccusage_no_records"

# AWF runtime -> ccusage per-source subcommand. Generic table; any runtime not
# present here has no ccusage source and reports ``ccusage_source_unsupported``.
# Grok Build is intentionally absent until ccusage supports an xAI/Grok source.
_PROVIDER_CCUSAGE_SOURCE: dict[AgentRuntime, str] = {
    AgentRuntime.claude_code: "claude",
    AgentRuntime.codex: "codex",
    AgentRuntime.gemini: "gemini",
    AgentRuntime.opencode: "opencode",
}

_INPUT_TOKEN_KEYS = ("inputTokens", "input_tokens")
_CACHED_INPUT_TOKEN_KEYS = (
    "cachedInputTokens",
    "cached_input_tokens",
)
_CACHED_INPUT_TOKEN_SPLIT_KEYS = (
    "cacheCreationTokens",
    "cacheReadTokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)
_OUTPUT_TOKEN_KEYS = ("outputTokens", "output_tokens")
_REASONING_OUTPUT_TOKEN_KEYS = ("reasoningOutputTokens", "reasoning_output_tokens")
_TOTAL_TOKEN_KEYS = ("totalTokens", "total_tokens")
_COST_KEYS = ("totalCost", "costUSD", "cost", "cost_estimate")


def provider_ccusage_source(runtime: AgentRuntime) -> str | None:
    """Return the ccusage source subcommand for an AWF runtime, or ``None``."""

    return _PROVIDER_CCUSAGE_SOURCE.get(runtime)


@dataclass(frozen=True)
class NormalizedUsage:
    """Normalized token/cost accounting extracted from ccusage output."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    cost_estimate: float | None = None
    currency: str | None = None
    model: str | None = None

    def as_baseline_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "cost_estimate": self.cost_estimate,
            "currency": self.currency,
            "model": self.model,
        }

    @classmethod
    def from_baseline_dict(cls, data: dict[str, Any]) -> NormalizedUsage:
        return cls(
            input_tokens=data.get("input_tokens"),
            cached_input_tokens=data.get("cached_input_tokens"),
            output_tokens=data.get("output_tokens"),
            reasoning_output_tokens=data.get("reasoning_output_tokens"),
            total_tokens=data.get("total_tokens"),
            cost_estimate=data.get("cost_estimate"),
            currency=data.get("currency"),
            model=data.get("model"),
        )


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first(mapping: dict[str, Any], keys: tuple[str, ...], coerce: Any) -> Any:
    for key in keys:
        if key in mapping:
            coerced = coerce(mapping[key])
            if coerced is not None:
                return coerced
    return None


def _sum(mapping: dict[str, Any], keys: tuple[str, ...], coerce: Any) -> int | None:
    """Sum a fixed list of numeric fields after coercion, ignoring missing keys."""

    total = 0
    seen = False
    for key in keys:
        if key not in mapping:
            continue
        coerced = coerce(mapping[key])
        if coerced is not None:
            total += coerced
            seen = True
    return total if seen else None


def _cached_input_tokens_from_record(record: dict[str, Any]) -> int | None:
    """Prefer the pre-aggregated cached token key when present."""

    cached_input_tokens = cast(int | None, _first(record, _CACHED_INPUT_TOKEN_KEYS, _coerce_int))
    if cached_input_tokens is not None:
        return cached_input_tokens
    return _sum(record, _CACHED_INPUT_TOKEN_SPLIT_KEYS, _coerce_int)


def _usage_from_record(record: dict[str, Any], *, model: str | None) -> NormalizedUsage | None:
    input_tokens = _first(record, _INPUT_TOKEN_KEYS, _coerce_int)
    cached_input_tokens = _cached_input_tokens_from_record(record)
    output_tokens = _first(record, _OUTPUT_TOKEN_KEYS, _coerce_int)
    reasoning_output_tokens = _first(record, _REASONING_OUTPUT_TOKEN_KEYS, _coerce_int)
    total_tokens = _first(record, _TOTAL_TOKEN_KEYS, _coerce_int)
    if total_tokens is None and (
        input_tokens is not None
        or output_tokens is not None
        or reasoning_output_tokens is not None
        or cached_input_tokens is not None
    ):
        # reasoning_output_tokens is a subset of output_tokens; do not add it
        # separately to avoid double-counting. When output_tokens is absent,
        # reasoning_output_tokens stands in as the output contribution.
        effective_output = (
            output_tokens
            if output_tokens is not None
            else (reasoning_output_tokens if reasoning_output_tokens is not None else 0)
        )
        total_tokens = (
            (input_tokens if input_tokens is not None else 0)
            + (cached_input_tokens if cached_input_tokens is not None else 0)
            + effective_output
        )
    cost_estimate = _first(record, _COST_KEYS, _coerce_float)
    if (
        input_tokens is None
        and cached_input_tokens is None
        and output_tokens is None
        and reasoning_output_tokens is None
        and total_tokens is None
        and cost_estimate is None
    ):
        return None
    return NormalizedUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
        cost_estimate=cost_estimate,
        currency="USD" if cost_estimate is not None else None,
        model=model,
    )


def _usage_from_daily(daily: list[Any], *, model: str | None) -> NormalizedUsage | None:
    totals: dict[str, Any] = {}
    seen = False
    for entry in daily:
        if not isinstance(entry, dict):
            continue
        usage = _usage_from_record(entry, model=model)
        if usage is None:
            continue
        seen = True
        if usage.input_tokens is not None:
            totals["inputTokens"] = (totals.get("inputTokens", 0)) + usage.input_tokens
        if usage.cached_input_tokens is not None:
            totals["cachedInputTokens"] = (
                totals.get("cachedInputTokens", 0) + usage.cached_input_tokens
            )
        if usage.output_tokens is not None:
            totals["outputTokens"] = (totals.get("outputTokens", 0)) + usage.output_tokens
        if usage.reasoning_output_tokens is not None:
            totals["reasoningOutputTokens"] = (
                totals.get("reasoningOutputTokens", 0) + usage.reasoning_output_tokens
            )
        if usage.total_tokens is not None:
            totals["totalTokens"] = (totals.get("totalTokens", 0)) + usage.total_tokens
        if usage.cost_estimate is not None:
            totals["totalCost"] = (totals.get("totalCost", 0.0)) + usage.cost_estimate
    if not seen:
        return None
    return _usage_from_record(totals, model=model)


def normalize_ccusage_json(raw: str) -> tuple[NormalizedUsage | None, str | None]:
    """Parse ccusage ``--json`` output into ``NormalizedUsage`` or a reason code."""

    text = raw.strip()
    if not text:
        # Callers only parse stdout after a clean (exit 0) ccusage run, so empty
        # output means "ran fine, no records yet", not unreadable JSON. Reporting
        # REASON_INVALID_JSON here would wrongly flag the run's baseline as
        # unavailable (see _capture_baseline) for every later sample.
        return None, REASON_NO_RECORDS
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, REASON_INVALID_JSON
    if not isinstance(data, dict):
        return None, REASON_INVALID_JSON

    model = data.get("model") if isinstance(data.get("model"), str) else None

    totals = data.get("totals")
    if isinstance(totals, dict):
        usage = _usage_from_record(totals, model=model)
        if usage is not None:
            return usage, None

    daily = data.get("daily")
    if isinstance(daily, list):
        usage = _usage_from_daily(daily, model=model)
        if usage is not None:
            return usage, None

    return None, REASON_NO_RECORDS


def _delta(current: int | None, baseline: int | None) -> int | None:
    if current is None:
        return None
    if baseline is None:
        return current
    return max(0, current - baseline)


def _delta_float(current: float | None, baseline: float | None) -> float | None:
    if current is None:
        return None
    if baseline is None:
        return current
    return max(0.0, current - baseline)


def subtract_baseline(
    current: NormalizedUsage, baseline: NormalizedUsage | None
) -> NormalizedUsage:
    """Return ``current - baseline`` per field, clamped at zero.

    When ``baseline`` is ``None`` the current reading is returned unchanged.
    ``None`` fields in ``current`` stay ``None`` (unknown, not zero).
    """

    if baseline is None:
        return current
    return NormalizedUsage(
        input_tokens=_delta(current.input_tokens, baseline.input_tokens),
        cached_input_tokens=_delta(current.cached_input_tokens, baseline.cached_input_tokens),
        output_tokens=_delta(current.output_tokens, baseline.output_tokens),
        reasoning_output_tokens=_delta(
            current.reasoning_output_tokens,
            baseline.reasoning_output_tokens,
        ),
        total_tokens=_delta(current.total_tokens, baseline.total_tokens),
        cost_estimate=_delta_float(current.cost_estimate, baseline.cost_estimate),
        currency=current.currency,
        model=current.model,
    )


# Strict typed accessors for deserializing a *persisted* snapshot. Unlike the
# lenient ``_coerce_*`` helpers (which silently drop bad fields from untrusted
# ccusage output), these raise ``TypeError`` on a wrong-typed field so a
# corrupted snapshot file is rejected wholesale by ``read_latest_usage_snapshot``
# rather than loaded as partially-bogus accounting data.
def _require_str(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("expected string or null")


def _require_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("expected int or null")
    if isinstance(value, int):
        return value
    raise TypeError("expected int or null")


def _require_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("expected float or null")
    return float(value)


@dataclass(frozen=True)
class UsageSnapshot:
    """A normalized, latest-wins usage snapshot for one workspace run."""

    workspace_id: str
    provider: str
    ccusage_source: str | None
    status: str
    phase: str
    captured_at: str
    reason: str | None = None
    # Agent run outcome (success/failed/timeout/cancelled/running) at the moment
    # this sample was taken. Distinct from ``status`` (sample availability): a
    # final snapshot after a timeout and one after success both carry
    # ``status="available"``/``phase="final"``, so the run outcome is the only
    # field that distinguishes them.
    run_status: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    cost_estimate: float | None = None
    currency: str | None = None
    baseline: dict[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION
    source: str = USAGE_SOURCE

    @property
    def has_metrics(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.cached_input_tokens,
                self.output_tokens,
                self.reasoning_output_tokens,
                self.total_tokens,
                self.cost_estimate,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "source": self.source,
            "ccusage_source": self.ccusage_source,
            "model": self.model,
            "status": self.status,
            "run_status": self.run_status,
            "reason": self.reason,
            "phase": self.phase,
            "captured_at": self.captured_at,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "cost_estimate": self.cost_estimate,
            "currency": self.currency,
            "baseline": self.baseline,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageSnapshot:
        baseline = data.get("baseline")
        if baseline is not None and not isinstance(baseline, dict):
            raise TypeError("baseline must be an object or null")
        return cls(
            workspace_id=data["workspace_id"],
            provider=data["provider"],
            ccusage_source=_require_str(data.get("ccusage_source")),
            status=data["status"],
            phase=data["phase"],
            captured_at=data["captured_at"],
            reason=_require_str(data.get("reason")),
            run_status=_require_str(data.get("run_status")),
            model=_require_str(data.get("model")),
            input_tokens=_require_int(data.get("input_tokens")),
            cached_input_tokens=_require_int(data.get("cached_input_tokens")),
            output_tokens=_require_int(data.get("output_tokens")),
            reasoning_output_tokens=_require_int(data.get("reasoning_output_tokens")),
            total_tokens=_require_int(data.get("total_tokens")),
            cost_estimate=_require_float(data.get("cost_estimate")),
            currency=_require_str(data.get("currency")),
            baseline=baseline,
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            source=data.get("source", USAGE_SOURCE),
        )

    def baseline_usage(self) -> NormalizedUsage | None:
        if not isinstance(self.baseline, dict):
            return None
        return NormalizedUsage.from_baseline_dict(self.baseline)


def workspace_usage_dir(work_dir: str | Path, workspace_id: str) -> Path:
    """Return the AWF-owned usage directory for a workspace."""

    return Path(work_dir) / "usage" / workspace_id


def _resolve_work_dir(work_dir: str | Path | None) -> Path:
    if work_dir is not None:
        return Path(work_dir)
    # Match the worker's work_dir normalization (build_worker_runtime applies
    # .expanduser().resolve()) so the API reader and the collector writer agree
    # on the snapshot directory even when settings.work_dir holds ~/ or a
    # relative/symlinked path.
    return Path(get_settings().work_dir).expanduser().resolve()


def write_usage_snapshot(snapshot: UsageSnapshot, *, work_dir: str | Path) -> Path:
    """Atomically write the latest snapshot for a workspace and return its path."""

    usage_dir = workspace_usage_dir(work_dir, snapshot.workspace_id)
    usage_dir.mkdir(parents=True, exist_ok=True)
    target = usage_dir / SNAPSHOT_FILENAME
    # A per-write unique suffix (not just the process-scoped PID) keeps the temp
    # path distinct if two coroutines in the same worker process write a
    # snapshot for the same workspace concurrently, so neither can overwrite the
    # other's bytes before ``replace`` performs the atomic swap.
    tmp = usage_dir / f"{SNAPSHOT_FILENAME}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(snapshot.to_dict(), separators=(",", ":")))
        tmp.replace(target)
    finally:
        # ``replace`` consumes tmp on success; this clears a partial temp left by
        # a failed write so the now-unique-named temps cannot accumulate.
        tmp.unlink(missing_ok=True)
    return target


def read_latest_usage_snapshot(
    workspace_id: str | None, *, work_dir: str | Path | None = None
) -> UsageSnapshot | None:
    """Read the latest usage snapshot for a workspace, or ``None`` if absent/invalid."""

    if not workspace_id:
        return None
    target = workspace_usage_dir(_resolve_work_dir(work_dir), workspace_id) / SNAPSHOT_FILENAME
    try:
        raw = target.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
        return UsageSnapshot.from_dict(data)
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        _log.warning("usage.snapshot.invalid", workspace_id=workspace_id)
        return None


def read_latest_usage_snapshots(
    workspace_ids: Iterable[str | None], *, work_dir: str | Path | None = None
) -> dict[str, UsageSnapshot | None]:
    """Read the latest snapshot for several workspaces in one pass.

    Designed to be offloaded to a worker thread (e.g. ``asyncio.to_thread``) so
    list endpoints can take the per-workspace blocking file reads off the async
    event loop. The work directory is resolved once and reused. Returns a map
    keyed by workspace id (blank/``None`` ids and duplicates are skipped); each
    value is the snapshot or ``None`` when absent/invalid.
    """

    resolved = _resolve_work_dir(work_dir)
    snapshots: dict[str, UsageSnapshot | None] = {}
    for workspace_id in workspace_ids:
        if not workspace_id or workspace_id in snapshots:
            continue
        snapshots[workspace_id] = read_latest_usage_snapshot(workspace_id, work_dir=resolved)
    return snapshots
