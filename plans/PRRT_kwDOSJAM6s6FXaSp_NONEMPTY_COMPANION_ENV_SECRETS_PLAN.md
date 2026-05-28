# Nonempty Companion Env Secrets Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FXaSp` reports that required companion
environment secrets accept empty source values and render Docker Compose
`${VAR?err}` interpolation, which only fails when the source is unset. This
allows an explicitly empty required secret to reach companion startup instead
of failing with `COMPANION_ENV_SECRET_SOURCE_MISSING`.

Scope is limited to env-backed companion secret resolution, required Compose
placeholder rendering, focused unit coverage, and the required plan/validation
artifacts for this review thread.

## Requirements Checklist

- Required companion env secrets with empty source values must fail with
  `COMPANION_ENV_SECRET_SOURCE_MISSING`.
- Required companion env secrets must render `${VAR:?err}` so persisted Compose
  files also fail when a source variable is unset or empty.
- Optional companion env secrets must keep the existing empty-value behavior.
- Raw secret values must not be placed into AWF service objects or rendered
  Compose YAML.
- Validation must stay focused; full AWF/GitHub validation remains managed
  after agent completion.

## Implementation Steps

1. Update the companion service regression coverage so an empty required source
   env value fails with the existing reason code.
2. Update required placeholder expectations in focused node tests to use
   Compose's unset-or-empty required form.
3. Implement the resolver and placeholder changes in
   `src/awf/node/companion_services.py`.
4. Run focused tests and touched-file lint only.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q -k "environment_secret"`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_resolves_companion_environment_secrets tests/unit/node/test_compose_manager.py::TestRender::test_dind_companion_environment_secret_placeholder_is_rendered_without_raw_value -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py`
  must pass.
- Do not run broad coverage, full repository suites, frontend builds, or
  CI-equivalent validation in this agent phase.
