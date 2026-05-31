# Comment 4396899384 Cursor Static Token Warning Validation

Plan reference:
`plans/COMMENT_4396899384_CURSOR_STATIC_TOKEN_WARNING_PLAN.md`

## Requirement Status

- Complete: Cursor env auth still returns `CURSOR_ENV_AUTH_PRESENT` with
  `static_env_token` scope and `service_env` isolation.
- Complete: Cursor env auth now emits a `STATIC_TOKEN_FALLBACK` warning using
  the same warning structure and wording pattern as other static env-token
  provider paths.
- Complete: The regression test still verifies the raw `CURSOR_API_KEY` secret
  value is absent from the serialized readiness payload.
- Complete: Focused tests demonstrate the new Cursor warning behavior.
- Complete: Broad AWF/GitHub-owned validation was not executed inside the agent
  phase.

## Evidence

Files changed:

- `src/awf/service/provider_readiness.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py`
- `plans/COMMENT_4396899384_CURSOR_STATIC_TOKEN_WARNING_PLAN.md`
- `plans/COMMENT_4396899384_CURSOR_STATIC_TOKEN_WARNING_VALIDATION.md`

Focused checks:

- Pre-fix TDD check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q`
  failed as expected on
  `test_provider_readiness_cursor_env_present` because
  `cursor["warnings"]` was `[]`.
- Post-fix check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q`
  passed with `65 passed`.
- Existing provider inference review-summary check:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py::test_cursor_provider_inference_takes_precedence_over_google_markers -q`
  passed with `1 passed`, confirming that separate review-summary concern is
  already covered locally.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/provider_readiness.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py`
  passed after focused formatting of the changed test file.

No broad AWF/GitHub validation suite, full coverage gate, frontend build, push,
rebase, or branch switch was run.
