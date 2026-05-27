# Fix Readyz Retained Worktree CI Validation

Plan reference: `plans/fix_readyz_retained_worktree_ci_PLAN.md`

## Requirement Status

- Reproduce or narrow the CI failure with focused pytest commands: Complete.
  The AWF-provided single-test repro passed locally, so the failure was narrowed
  to CI-mode load/scheduling rather than an isolated deterministic assertion.
- Identify the root cause behind the terminal retained-worktree readiness
  result: Complete. The CI log showed `/readyz` returning 503 with an asyncpg
  cancellation during the call. The part-002 tests cover Docker/orphan-resource
  readiness but also exercised unrelated real DB health and egress summary
  checks under xdist coverage load.
- Add or adjust a regression test when behavior changes: Complete. No
  production behavior changed; the existing failing readiness test now isolates
  the DB and egress checks that are covered in part 001.
- Implement the smallest code/test fix consistent with existing patterns:
  Complete. `tests/unit/api/test_health_parts/test_health_part_002.py` now
  stubs only the unrelated DB health and egress summary checks in its local
  fixture while leaving workspace DB classification intact.
- Run focused verification only: Complete. Full AWF/GitHub validation and full
  coverage are intentionally left to AWF after agent completion.
- Commit the fix locally: Complete. This validation file is included in the
  local fix commit.

## Evidence

Files changed:

- `tests/unit/api/test_health_parts/test_health_part_002.py`
- `plans/fix_readyz_retained_worktree_ci_PLAN.md`
- `plans/fix_readyz_retained_worktree_ci_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_only_retained_worktree_stays_healthy -q`
  - Result: Passed before and after the fixture isolation change.
- `uv run --python 3.12 --extra dev ruff check tests/unit/api/test_health_parts/test_health_part_002.py`
  - Result: Passed.
- `uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope tests/unit/api/test_health_parts/test_health_part_002.py -q`
  - Result: Passed, `7 passed`.
- `uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/api/test_health_parts/test_health_part_002.py -q`
  - Result: Passed, `56 passed`.

Full repository tests, full coverage, frontend builds, and CI-equivalent gates
were not run locally per the AWF workspace contract; AWF/GitHub own broad
validation, provenance, logs, timeouts, and merge gating after this agent phase.
