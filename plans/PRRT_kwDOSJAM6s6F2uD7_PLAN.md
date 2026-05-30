# PRRT_kwDOSJAM6s6F2uD7 Plan

## Problem Statement

The owned-path helper currently treats `docs/awf-plans` and every child path as
an internal AWF plan artifact. That hides real conflicts for the tracked
`docs/awf-plans/README.md`, including inter-workspace owned-path checks and
target-branch staleness.

## Scope

- Narrow internal plan artifact classification to generated `ws_*` plan and
  conformance artifact filenames under `docs/awf-plans/`.
- Keep generated plan/conformance artifact owned paths out of inter-workspace
  dependency comparisons.
- Preserve staleness behavior where target changes to generated plan artifacts
  are advisory and non-blocking.
- Add regressions proving `docs/awf-plans/README.md` remains an ordinary tracked
  documentation path.

## Requirements Checklist

- `docs/awf-plans/README.md` is not classified as an internal artifact.
- Broad directory ownership such as `docs/awf-plans/**` is not silently removed
  from inter-workspace comparisons.
- Generated artifact paths/globs such as `docs/awf-plans/ws_123.md`,
  `docs/awf-plans/ws_123.conformance.json`, and `docs/awf-plans/ws_*.md` remain
  internal.
- Staleness records README target changes as blocking overlap, while generated
  plan/conformance target changes remain advisory.
- Focused tests and lint cover the touched helper and behavior.

## Implementation Steps

1. Add failing tests in `tests/unit/common/test_owned_paths.py`,
   `tests/unit/service/test_staleness_parts/test_staleness_part_001.py`, and
   repository/overlap tests that cover generated artifact filtering plus README
   conflict preservation.
2. Update existing plan-artifact inter-workspace tests to use generated
   `ws_*` artifact globs instead of the broad `docs/awf-plans/**` directory
   glob where they are asserting generated artifact behavior.
3. Narrow `src/awf/common/owned_paths.py` to classify only generated `ws_*`
   plan/conformance artifact filenames or matching generated artifact globs.
4. Update any local docstrings that still describe every child of
   `docs/awf-plans/` as advisory.

## Verification

- First run a targeted failing test before implementation where practical.
- Run focused unit tests for:
  - `tests/unit/common/test_owned_paths.py`
  - affected staleness tests
  - affected inter-workspace overlap tests
- Run focused `ruff check` on touched Python files.
- Do not run the full AWF/GitHub validation suite; AWF owns broad validation
  after the agent phase.
