# T07 — Provider Setup Orchestration With GitHub First-Class (Plan)

Authoritative implementation contract: `docs/awf-plans/ws_23a0b3e2fa15420c826d4b86.md`
(saved by AWF for this workspace). This file is the `plans/`-local pointer +
requirements checklist required by `plans/PLAN_EXECUTION_PROTOCOL.md`.

## Problem statement & scope

Add `src/awf/host_setup/providers.py` to orchestrate provider setup on top of T04
(setup `--provider` selector + readiness payload aggregation) and T06 (credential
backend abstraction). The orchestration captures/discovers a credential, converts
it to a **safe reference** (never a raw value), rechecks the provider with the
bounded probes in `provider_readiness`, and returns a renderable, secret-free
summary plus an updated `HostSetupConfig`. GitHub is first-class (ready via `gh`
or env ref, no raw-token storage). Wire it into the `awf setup` non-dry-run
dispatch.

## Requirements checklist

1. GitHub ready via `gh auth status` **or** an env ref (`GH_TOKEN`/`GITHUB_TOKEN`),
   never storing a raw token.
2. One failed provider marks only that provider unavailable; others unaffected.
3. Provider readiness summary renderable by setup (and compatible with start).
4. Provider setup network probes are bounded (positive timeout).
5. Selected-provider setup leaves unselected providers unchanged and labels the
   summary `targeted_recheck` (vs `all_providers`).
6. Captured/discovered credentials become refs consumed by readiness rechecks.
7. No raw secret values in stdout/stderr/config/logs/test snapshots.
8. AWF Cloud is a deterministic stub slot; `docker` stays out of the surface.
9. New CLI dispatch replaces the T04 placeholder; minimal, reuses persistence.

## Implementation steps

- Add `check_single_provider_readiness` seam to `service/provider_readiness.py`
  (probe one provider only — needed so a targeted recheck never probes others).
- New `host_setup/providers.py`: frozen `ProviderSpec` registry, `ProviderSetupResult`
  / `ProviderSetupSummary` (`extra="forbid"`, frozen), `orchestrate_provider_setup`,
  `render_provider_summary`, GitHub-first-class helper, credential→ref conversion,
  per-provider recheck, non-blocking failure isolation, interactive-input signal.
- Export new public symbols from `host_setup/__init__.py`.
- `cli/setup_commands.py`: on non-dry-run + host-ready, call
  `orchestrate_provider_setup`, fold `to_details()` into `details["providers"]`,
  persist the returned config; fire the interactive guard only when a *selected*
  provider needs capture under `--non-interactive`.

## Verification commands & pass criteria

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/providers.py
uv run --python 3.12 --extra dev mypy
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_host_setup_providers.py \
  tests/unit/service/test_provider_readiness.py \
  tests/unit/cli/test_setup_commands.py -q
```

Pass criteria: all focused tests green; `providers.py` 100% line/branch under the
focused suite; no new reason codes; AWF/CI owns the full 99% gate, OpenAPI drift,
and console build post-completion.
