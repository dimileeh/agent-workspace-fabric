# PRRT_kwDOSJAM6s6FYsG Compose Build Budget Plan

## Problem Statement and Scope

The review thread points out that `docker compose up --wait --wait-timeout <N>`
uses `<N>` for Compose readiness, while AWF also caps the whole subprocess at
`<N> + 60`. Cold image builds or container recreation can consume most of that
outer cap before Compose has had the requested readiness wait budget.

Scope is limited to ComposeManager timeout calculation and focused regression
coverage for initial launch and PR-monitor resume launch.

## Requirements Checklist

- Keep `--wait-timeout` equal to the effective `compose_up_timeout_seconds`.
- Budget Docker Compose build/recreate/start time separately from Compose's
  readiness wait.
- Apply the same outer timeout policy to `up()` and `ensure_project_up()`.
- Preserve structured `DOCKER_COMMAND_TIMEOUT` failures for genuinely hung
  compose subprocesses.
- Run only focused checks; full AWF/GitHub validation remains post-agent owned.

## Implementation Steps

1. Add failing regression coverage in `tests/unit/node/test_compose_manager_subprocess.py`
   for the outer timeout calculation used by `up()` and `ensure_project_up()`.
2. Add a small helper/constant in `src/awf/node/compose_manager.py` that computes
   the outer compose-up capture timeout from a separate build/start budget plus
   the Compose readiness wait budget and existing buffer.
3. Wire both compose-up entry points through the helper.
4. Run the focused regression tests and update validation evidence.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager_subprocess.py -q -k "compose_timeout or ensure_project_up"`

Pass criteria: the targeted compose-manager subprocess tests pass and show the
outer timeout exceeds the configured readiness wait by a separate build/start
budget plus the existing buffer. Full AWF/GitHub validation is managed after
agent completion.
