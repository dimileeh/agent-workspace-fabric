# Workspace LLM Usage Collector Plan

## Summary

AWF console renders "LLM usage unavailable / usage_not_reported" because
`workspace_usage_summary` only ever saw provider usage metadata that an
operation happened to attach to its payload/result. No AWF component actively
measured per-run LLM usage inside the workspace agent container.

This plan adds an AWF-owned usage collector that samples `ccusage` from inside
the workspace agent container while agent work runs, normalizes the output into
safe numeric accounting data, persists a latest-wins snapshot under the AWF
`work_dir`, and surfaces it through the existing observability/API payload so the
console shows trusted live and final usage. The collector is generic across all
currently supported AWF providers (`claude_code`, `codex`, `gemini`,
`opencode`), keeps operation-usage aggregation as a compatibility fallback, and
never persists raw JSONL, file paths, or credential-bearing files.

It is wired around the single shared agent execution chokepoint
(`AgentAdapter.run`) so normal workspace execution and PR-monitor/recovery agent
runs are covered without duplicating provider-specific runner code.

## Design (as built)

### Runtime image
- `docker/agent-runtime.Dockerfile` pins `ccusage@20.0.3` via the existing
  `npm install -g` layer (no runtime `npx`/`bunx` network fetch), with a
  `ccusage --version` smoke line.

### Provider → ccusage source mapping
- `usage_store.provider_ccusage_source` maps AWF `AgentRuntime` to a ccusage
  per-source subcommand: `claude_code→claude`, `codex→codex`, `gemini→gemini`,
  `opencode→opencode`. Any runtime not in the table has no source and reports
  `ccusage_source_unsupported` (graceful, never a crash).

### Collector (`service/usage_collection.CcusageCollector`)
- Implements the `adapters.usage.UsageSampler` protocol (a leaf module depending
  only on stdlib + `awf.db.enums`, so `adapters.base` can accept a sampler
  without an adapter→service import cycle).
- Invokes `ccusage <source> daily --json --offline` through the tracked compose
  exec, with bounded wall + idle command timeouts.
- Captures a baseline at run start, samples every 60 seconds while the agent run
  is active, and takes a final sample on every exit path.
- Reports baseline-subtracted deltas (clamped at zero) so copied host history
  and prior runs cannot inflate per-run totals. The baseline is persisted in the
  snapshot and reused across runs of the same workspace.

### Finalization / outcome safety (`adapters/base.AgentAdapter.run`)
- `run()` wraps a refactored `_run_agent_cli()` with `_start_usage_sampling` and
  `_finalize_usage_sampling` in `try/except/finally`, mapping success /
  `AgentRunError` (timeout vs failed) / `CancelledError` to a final status.
- `finalize` uses `asyncio.shield` so the final sample completes even when the
  agent task is being cancelled. Sampling errors are reason-coded or
  swallowed-and-logged and never mask the agent outcome.

### Persistence (`service/usage_store`)
- Single latest-wins snapshot per workspace at
  `<work_dir>/usage/<workspace_id>/snapshot.json`, written atomically (temp +
  `replace`).
- Stores only normalized numeric/accounting data plus safe metadata: provider,
  ccusage source, model when available, status, phase, reason code, timestamps,
  token counts, cost estimate, currency, and the baseline dict. No raw JSONL, no
  file paths, no secret-bearing content.
- The API reader and the worker writer normalize `work_dir` identically
  (`.expanduser().resolve()`) so they always agree on the snapshot directory.

### Claude/Gemini auth isolation (`node/auth_mounts`)
- Per-workspace auth copies exclude provider usage-history directories via
  `shutil.ignore_patterns` (`projects`, `todos`, `shell-snapshots`, `statsig`
  for Claude; `tmp` for Gemini), so copied host history cannot be attributed to
  the workspace run. Baseline/delta is the second line of defence.

### Observability tiers (`service/workspace_observability.workspace_usage_summary`)
1. ccusage snapshot with metrics → trusted live/final usage.
2. Operation-usage aggregation → compatibility fallback (preserved).
3. ccusage snapshot without metrics → surface its reason code.
4. `usage_not_reported`.

### Console (focused, no redesign)
- `apps/console/lib/format.ts` adds `formatUsageProvenance(source, reason)` with
  friendly source/reason label maps.
- `apps/console/components/console-dashboard.tsx` uses it in both the unavailable
  and available branches of `UsageSummaryBlock`.

## Normalizer reason codes
- `ccusage_source_unsupported`, `ccusage_unavailable`, `ccusage_command_failed`,
  `ccusage_timeout`, `ccusage_invalid_json`, `ccusage_no_records`.
- `normalize_ccusage_json` reads `totals` first, then sums `daily`, tolerates
  missing fields and synthesizes totals, ignores boolean/non-numeric token
  values, and returns an explicit reason on empty/invalid input.

## Test Plan
- Provider/source mapping for all supported runtimes (claude_code, codex,
  gemini, opencode) and unknown-runtime → `None`.
- Claude/Gemini auth isolation excludes host usage-history directories.
- Baseline/delta excludes pre-run usage and reuses a persisted baseline across
  runs; fresh capture when no prior baseline exists.
- 60-second polling cadence while the run is active; live snapshots written
  during the run.
- Final sample runs in success, failure, timeout, and cancellation paths without
  masking the agent outcome.
- Parser/normalizer: valid totals/daily, empty/no-records, command failure,
  timeout, malformed JSON → explicit reason codes.
- Observability prefers ccusage snapshots, falls back to operation usage, and
  surfaces snapshot reason codes; `work_dir` normalization parity.
- Console renders available totals and friendly unavailable reason states.

## Risks, Assumptions, Non-goals
- **Risk:** the exact ccusage multi-source CLI surface (`<source> daily --json
  --offline`) must hold for the pinned `20.0.3`; unsupported combinations
  degrade to reason codes, not crashes. CLI-surface verification needs the built
  runtime image (AWF/CI-owned at build time).
- **Risk:** API reader and worker writer must share the same physical
  `work_dir` volume, exactly as the existing `LogStore` already assumes.
- **Assumption:** snapshots live under `<work_dir>/usage/<workspace_id>/`,
  outside the repo, so they are never committed and need no `.gitignore` entry.
- **Non-goals:** no dashboard redesign, no new public REST schema fields
  (reuse `LlmUsageSummary`), no removal of operation-usage aggregation, no DB
  migrations (snapshots are file-backed), and no persistence of raw JSONL or
  secret-bearing files.
