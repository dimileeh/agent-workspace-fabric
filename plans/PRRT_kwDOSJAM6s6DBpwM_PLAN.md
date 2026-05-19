# PRRT_kwDOSJAM6s6DBpwM Plan

## Problem Statement and Scope

PR #264 review thread `PRRT_kwDOSJAM6s6DBpwM` reports that no-path `awf init`
can drop an env seeding failure when Docker preflight exits before bootstrap.
The current CLI only reports the pretty env warning after Docker preflight has
already succeeded, and the JSON Docker failure payload does not attach
`env_error`.

Scope is limited to `awf init` bootstrap-mode preflight failure reporting and
focused CLI regression coverage.

## Requirements Checklist

- [ ] Preserve the existing Docker preflight failure reason, message, action,
  and exit code.
- [ ] Include `env_error` in JSON Docker preflight failures when env seeding
  failed before preflight.
- [ ] Emit the pretty env seeding warning before Docker preflight can exit.
- [ ] Avoid duplicate pretty warnings on successful bootstrap.
- [ ] Add regression tests for JSON and pretty Docker preflight failures.
- [ ] Run focused validation for the touched CLI tests.

## Implementation Steps

1. Add failing CLI unit tests covering env write failure followed by Docker
   preflight failure in JSON and pretty output modes.
2. Update `_run_init_service_bootstrap` so the pretty env warning is emitted
   before local preflight exits and so JSON preflight failure payloads include
   `env_error`.
3. Run the focused new tests, then the relevant `tests/unit/cli/test_init.py`
   suite and lint for the touched files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_json_includes_env_error_when_docker_preflight_fails tests/unit/cli/test_init.py::test_init_without_path_warns_when_env_write_and_docker_preflight_fail -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py
```

Pass criteria: commands exit 0, JSON Docker failure output contains the original
env seeding error, and pretty output contains one env warning before the Docker
failure notice.
