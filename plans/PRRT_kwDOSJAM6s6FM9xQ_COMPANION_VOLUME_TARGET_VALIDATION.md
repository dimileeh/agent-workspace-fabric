# PRRT_kwDOSJAM6s6FM9xQ Companion Volume Target Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FM9xQ_COMPANION_VOLUME_TARGET_PLAN.md`

## Requirement Status

- Reject non-absolute companion volume targets: Complete.
- Reject companion volume targets containing `:`: Complete.
- Preserve valid named-volume and repo-relative source behavior: Complete.
- Add regression coverage for the review examples: Complete.
- Run focused checks only during the agent phase: Complete.

## Evidence

Changed files:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `plans/PRRT_kwDOSJAM6s6FM9xQ_COMPANION_VOLUME_TARGET_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FM9xQ_COMPANION_VOLUME_TARGET_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Expected initial failure before implementation: 2 new invalid target cases
    did not raise `ValidationError`.
  - Final result: Passed, 31 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Passed.

Full AWF/GitHub validation, whole-repository test suites, frontend builds, and
coverage gates were not run in the agent phase; AWF owns those broad validation
gates after agent completion.

## Gaps

None.
