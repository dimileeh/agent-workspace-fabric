# PRRT_kwDOSJAM6s6CMi6i Setup Retry Budget Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6CMi6i` reports that
`setup_dependency_network.retry_budget` is populated with the sum of the setup
dependency retry budget and the generic flaky validation retry budget. Setup
dependency retry exhaustion is decided only by the setup dependency budget, so
the structured metadata can report `retry_exhausted=true` while
`retry_count < retry_budget` when a profile also configures flaky retries.

Scope is limited to runtime validation setup dependency metadata and focused
regression coverage.

## Requirements Checklist

- Add regression coverage proving setup dependency retry exhaustion reports the
  setup dependency retry budget, not the combined setup plus flaky retry budget.
- Keep final `ValidationCommandResult.retry_count` as the combined command retry
  count used by validation provenance.
- Keep setup dependency attempt lineage, recovery metadata, and retry exhausted
  reason behavior unchanged.
- Update existing mixed setup/flaky retry expectations so metadata budget scope
  matches setup dependency retry semantics.
- Validate with focused runtime validation tests and lint on touched files.

## Implementation Steps

1. Add a failing runtime regression for setup dependency exhaustion when
   `profile.validation.retry_budget` is non-zero.
2. Update the existing setup/flaky retry metadata assertion to expect the
   setup dependency retry budget.
3. Pass `self._setup_retry_budget` into all `setup_dependency_network` metadata
   call sites.
4. Run the focused regression tests, then the relevant runtime validation test
   module and ruff check.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget tests/unit/runtime/test_validation.py::test_setup_dependency_network_exhaustion_reports_setup_retry_budget_only -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py
```

Pass criteria: focused regressions pass, runtime validation tests pass, ruff
reports no issues, and the behavior change is limited to the metadata budget
field.
