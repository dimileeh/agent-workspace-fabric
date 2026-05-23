# Workspace LLM Usage Collector Validation

## Context

This workspace is a timeout-salvage continuation (source reason
`AGENT_IDLE_TIMEOUT`). AWF restored the prior agent's diff; this run continued
from the recovered, substantially-complete implementation. Per the AWF
workspace contract, broad/coverage/e2e validation (full
`pytest --cov-fail-under`, repo-wide ruff/mypy, `pre-commit`, the Playwright
suite, and image builds) is owned by AWF + GitHub CI after the agent phase and
was deliberately not run locally (the prior idle timeout is consistent with a
long-running broad/coverage run). The checks below are focused on the changed
files only.

## Plan Alignment
- Pinned `ccusage@20.0.3` in `docker/agent-runtime.Dockerfile` via `npm install
  -g` with a `ccusage --version` smoke line (no runtime npx/bunx fetch).
- Added `adapters/usage.py` (`UsageSampler`/`UsageSampleContext` leaf protocols),
  `service/usage_store.py` (provider→source map, normalizer, baseline/delta,
  durable latest-wins snapshot, reason codes), and
  `service/usage_collection.py` (`CcusageCollector`: baseline capture, 60s loop,
  shielded finalize, bounded timeouts).
- Wired sampling into the shared chokepoint `AgentAdapter.run` and threaded
  `usage_sampler` through `WorkspaceExecutor` (all three agent call sites) and
  `build_worker_runtime`, covering normal execution and PR-monitor/recovery.
- Excluded provider usage-history directories from per-workspace Claude/Gemini
  auth copies in `node/auth_mounts.py`.
- Tiered `workspace_usage_summary`: ccusage-with-metrics → operation fallback →
  ccusage reason code → `usage_not_reported`; preserved operation aggregation.
- Focused console change: `formatUsageProvenance` in `lib/format.ts`, used in
  both branches of `UsageSummaryBlock`.
- Hardened `usage_store._resolve_work_dir` to normalize the default `work_dir`
  with `.expanduser().resolve()`, matching the worker's normalization so the API
  reader and collector writer agree on the snapshot directory (G3).

## Verification (focused)
- `uv run --python 3.12 --extra dev pytest -q tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/adapters/test_base_usage_collection.py tests/unit/node/test_service_auth_mounts.py tests/unit/service/test_worker.py tests/unit/service/test_workspaces_observability.py`
  - Result: passed, 212 tests.
- TDD for G3: ran the new
  `tests/unit/service/test_usage_store.py::test_read_latest_usage_snapshot_normalizes_user_home_work_dir`
  against the unpatched `_resolve_work_dir` first — failed (reader returned
  `None` for a `~/`-prefixed `work_dir`); after the one-line normalization it
  passed.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/service/test_usage_store.py`
  - Result: passed, 35 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/usage.py src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/adapters/base.py src/awf/control/executor.py src/awf/service/worker.py src/awf/node/auth_mounts.py src/awf/service/workspace_observability.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/adapters/test_base_usage_collection.py`
  - Result: passed (All checks passed!).
- `uv run --python 3.12 --extra dev mypy src/awf/adapters/usage.py src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/adapters/base.py src/awf/service/worker.py src/awf/service/workspace_observability.py`
  - Result: passed (no issues found in 6 source files).
- `node --test lib/format.test.mjs` (run from `apps/console`)
  - Result: passed, 15 tests.

## Required-behavior → covering tests
- Provider/source map (claude_code, codex, gemini, opencode):
  `test_usage_store::test_provider_ccusage_source_maps_all_supported_runtimes`,
  `test_usage_collection::test_ccusage_argv_per_provider`.
- Claude/Gemini auth isolation excludes host history:
  `test_service_auth_mounts::test_*_exclude_*_usage_history`.
- Baseline/delta excludes pre-run usage; baseline reused:
  `test_usage_collection::test_baseline_subtracted_from_final_sample`,
  `::test_persisted_baseline_reused_across_runs`,
  `::test_prior_snapshot_without_baseline_captures_fresh`.
- 60s cadence while active:
  `test_usage_collection::test_samples_at_sixty_second_interval_while_active`,
  `::test_live_snapshots_written_during_run`.
- Final sample in success/failure/timeout/cancellation, no outcome masking:
  `test_base_usage_collection::test_sampler_finalized_*` and
  `::test_sampler_*_does_not_mask_*`;
  `test_usage_collection::test_finalize_completes_final_sample_under_cancellation`.
- Parser reason codes (valid/empty/failure/timeout/malformed):
  `test_usage_store::test_normalize_ccusage_json_*`,
  `test_usage_collection::test_final_sample_reason_codes`.
- Observability prefers ccusage, falls back to operations, `work_dir` parity:
  `test_workspaces_observability::test_workspace_usage_summary_*`,
  `test_usage_store::test_read_latest_usage_snapshot_normalizes_user_home_work_dir`.
- Console renders totals + friendly unavailable reasons:
  `format.test.mjs::formatUsageProvenance*`,
  `dashboard-usage.spec.ts` ccusage cases.

## Deferred to AWF + GitHub CI (not run locally)
- Full `pytest --cov=awf --cov-report=term-missing --cov-fail-under` suite.
- Repo-wide ruff/mypy and `pre-commit`.
- Playwright e2e (`apps/console/tests/dashboard-usage.spec.ts`).
- `docker build` of `docker/agent-runtime.Dockerfile` and runtime `ccusage`
  CLI-surface smoke (`ccusage <source> daily --json --offline`), which requires
  the built image.
