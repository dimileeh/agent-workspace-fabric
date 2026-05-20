# DOCKER_HOST Logs Clear Plan

## Problem Statement And Scope

An unresolved PR review thread reports that `awf service logs` ignores an
explicit blank `DOCKER_HOST` in the resolved service environment. When the
caller process has a stale `DOCKER_HOST`, the logs subprocess may inherit that
stale value instead of clearing Docker host selection.

Scope is limited to local service logs environment construction and the
regression test for that behavior.

## Requirements Checklist

- Add a regression test proving `service_environ={"DOCKER_HOST": ""}` clears a
  stale caller `DOCKER_HOST`.
- Preserve existing behavior for non-empty `AWF_DOCKER_HOST` and `DOCKER_HOST`
  values.
- Keep secret filtering and Compose interpolation behavior unchanged.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_logs.py`.
2. Update `src/awf/service/logs.py` so blank service `DOCKER_HOST` is treated as
   an explicit caller-env scrub signal.
3. Run the focused logs unit test file.
4. Run the repository's narrow Python validation commands that cover the change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if practical for this workspace.
