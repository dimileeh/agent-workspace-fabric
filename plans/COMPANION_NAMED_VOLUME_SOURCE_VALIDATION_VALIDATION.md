# Companion Named Volume Source Validation

Plan reference: `COMPANION_NAMED_VOLUME_SOURCE_VALIDATION_PLAN.md`

## Requirement Status

- Complete: Reject companion named volume sources that contain `:`, whitespace,
  newlines, or other characters outside a conservative Docker-safe volume-name
  pattern.
- Complete: Preserve accepted companion volume sources for repo-relative mounts
  such as `./fixtures` and safe named volumes such as `api-cache`.
- Complete: Keep existing volume target validation unchanged.
- Complete: Run only focused validation commands for the changed behavior.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `plans/COMPANION_NAMED_VOLUME_SOURCE_VALIDATION_PLAN.md`
- `plans/COMPANION_NAMED_VOLUME_SOURCE_VALIDATION_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Result: passed, `33 passed in 0.44s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Result: passed.

Full AWF/GitHub validation was not executed in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after agent completion.
