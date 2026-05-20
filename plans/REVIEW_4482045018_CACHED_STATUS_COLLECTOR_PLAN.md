# Review 4482045018 Cached Status Collector Plan

## Problem Statement and Scope

PR review comment `issue:4482045018` reports that the cached service-status
collector used by `awf init <path>` only accepts the older doctor collector
keyword set. If the doctor path later forwards readiness-style context
(`environ`, `compose_file`, or `compose_env_file`) to the injected status
collector, project onboarding would raise `TypeError` instead of reusing the
already-collected service status.

Scope is limited to the path-mode `awf init` cached status collector and a
focused regression test.

## Requirements Checklist

- Add a regression test that exercises the cached status collector with
  readiness-style kwargs.
- Keep the cached collector behavior unchanged: it returns the already-started
  status task result and preserves failure-to-status conversion.
- Do not weaken existing doctor/readiness/status tests.
- Run the narrow relevant test command.
- Commit the local fix with a conventional commit message referencing the
  review comment id.

## Implementation Steps

1. Add a unit test near the existing `awf init <path>` cached status tests that
   calls the injected `status_collector` with `environ`, `compose_file`, and
   `compose_env_file`.
2. Confirm the new test fails before implementation when practical.
3. Update `_collect_cached_service_status` in `src/awf/cli/main.py` to accept
   and ignore extra keyword arguments.
4. Re-run the focused test file or focused tests.
5. Create `plans/REVIEW_4482045018_CACHED_STATUS_COLLECTOR_VALIDATION.md`
   with requirement-by-requirement evidence.
6. Stage only changed files and commit locally.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
```

Pass criteria: the command exits successfully, including the new regression
test and existing path-mode init coverage.
