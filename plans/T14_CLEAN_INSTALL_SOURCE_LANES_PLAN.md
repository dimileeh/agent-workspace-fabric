# T14 Clean-Install And Source-Lane Smoke Harness Plan

## Problem Statement And Scope

Implement T14 from `TODO/awf-full-installer-first-run-setup-backlog.md`: add a
focused first-run smoke harness that proves installer and source checkout lanes
without requiring a published release by default. The harness must keep
destructive or network-dependent behavior behind fixture/local-artifact gates,
and it must prove source checkout setup/start/help paths work from outside the
normal developer shell assumptions.

This plan follows the saved workspace contract in
`docs/awf-plans/ws_03f9dfb2e6254c05a8cad2a2.md`. AWF owns branch management,
push, PR creation, broad validation, and full coverage after agent completion.

## Requirements Checklist

- Add `scripts/first_run_smoke.py` with lanes for `installer-fixture`,
  `installer-release`, `tool-install`, `source-tool-install`, and
  `source-uv-run`.
- Default local runs must not require a published release or live network.
- Release smoke must be disabled unless `--allow-release` and explicit local
  release artifacts are provided.
- Installer fixture smoke must use local `file://` manifest/artifact data and
  `packaging/install.sh --dry-run`.
- Tool install environments must be isolated under a temporary smoke root for
  `uv` and `pipx`, including tool homes, tool bin directories, `HOME`, and
  `PATH`.
- Source checkout copy must preserve required AWF source markers and exclude
  `.git`, virtualenvs, caches, build/dist output, and other heavy dev state.
- Source no-global lane must run from outside the checkout with a temp `HOME`,
  scrubbed `PYTHONPATH`, explicit `--source-checkout`, and `uv run`.
- Source global lane must install from a temp copied checkout with
  `uv tool install . --force` into isolated tool directories, then run `awf`
  from outside the checkout.
- Add focused unit and integration tests for lane planning, command/env wiring,
  source copy behavior, release gating, and source-lane execution.
- Do not edit broad docs unless required for acceptance; T15 owns broader docs.

## Implementation Steps

1. Write failing tests first:
   - `tests/unit/scripts/test_first_run_smoke.py` for fixture installer command
     wiring, release gating, isolated tool envs, source copy ignore behavior,
     and source lane command construction.
   - `tests/integration/test_first_run_smoke.py` for source no-global and
     source global lane behavior with environmental skips only for unavailable
     local tooling/dependency resolution.
2. Implement `scripts/first_run_smoke.py` with small testable helpers:
   lane parsing, smoke result records, isolated env builders, fixture manifest
   generation, source checkout copy, subprocess execution, environmental
   failure classification, and CLI reporting.
3. Reuse `scripts/release_smoke.py` helpers for local release artifact manifest
   rewriting and installer dry-run invocation where practical.
4. Keep all lane side effects under temp directories unless the caller passes
   `--keep-temp`; never write host shell rc files or user tool homes.
5. Run targeted tests and lint/format checks for the changed files only.
6. Create `plans/T14_CLEAN_INSTALL_SOURCE_LANES_VALIDATION.md` with
   requirement-by-requirement status and focused command evidence.
7. Commit the implementation locally on the current AWF branch.

## Verification Commands And Pass Criteria

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_release_smoke.py -q
uv run --python 3.12 --extra dev pytest tests/integration/test_first_run_smoke.py -q
uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py tests/integration/test_first_run_smoke.py
uv run --python 3.12 --extra dev ruff format --check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py tests/integration/test_first_run_smoke.py
uv run --python 3.12 --extra dev python scripts/first_run_smoke.py --lane installer-fixture --lane source-uv-run --checkout-root .
```

Pass criteria:

- Unit tests pass and prove non-mutating fixture/release gating behavior.
- Integration tests pass or skip only for documented environmental local
  tooling/dependency gaps.
- Lint and format checks pass for changed harness/test files.
- The harness CLI can run fixture and source no-global lanes locally without
  publishing a release.
- Full AWF/GitHub validation remains deferred to AWF after agent completion.
