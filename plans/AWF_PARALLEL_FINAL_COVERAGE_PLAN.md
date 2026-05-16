# AWF Parallel Final Coverage Plan

## Problem

AWF self-dogfood needs deterministic workspace-local full coverage support for
explicit final gates without moving full coverage into ordinary targeted edit
validation. A coverage-wrapped pytest command can report at least the required
coverage while pytest exits nonzero because tests failed, errored, or timed out;
AWF must classify that as a validation/test failure with actionable evidence.

## Scope

- Keep the AWF self-profile coverage target at 99%.
- Preserve GitHub Actions as the authoritative full coverage gate for
  PR-monitored workspaces.
- Keep targeted edit validation from running local full coverage.
- Make coverage-wrapped pytest failures classify as validation failures, not
  infrastructure, stale, or cleanup failures.
- Fix or explicitly isolate only proven xdist/shared-state determinism gaps.

## Requirements Checklist

- Add regression coverage before implementation changes where practical.
- Capture failing pytest node IDs and bounded evidence when coverage passes but
  pytest exits nonzero.
- Ensure executor reason codes and failure messages prioritize pytest failure
  evidence over coverage success.
- Run workspace-local coverage only for explicit local coverage final gates with
  a declared coverage command.
- Preserve existing GitHub Actions final gate semantics.
- Document local validation and whether full parallel coverage was run locally.

## Implementation Steps

1. Inspect current validation and executor coverage paths.
2. Add focused regression tests for coverage-wrapped pytest failure parsing,
   executor reason-code/message propagation, and final-gate local coverage
   behavior.
3. Implement the smallest parser and executor changes needed to satisfy those
   tests.
4. Diagnose xdist determinism with the focused and, if feasible, full parallel
   coverage command; add deterministic cleanup or explicit serial grouping only
   if evidence requires it.
5. Run focused tests plus ruff and mypy.
6. Create `plans/AWF_PARALLEL_FINAL_COVERAGE_VALIDATION.md` with requirement
   status, changed files, commands run, and any remaining local/full-coverage
   caveat.

## Verification

Focused:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_executor_validation_fix_cycle.py -q
```

Static:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Parallel coverage stress when runtime permits:

```bash
uv run --python 3.12 --extra dev pytest -n 3 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99
```
