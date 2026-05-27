# PRRT_kwDOSJAM6s6FL6d3 Companion Volume Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FL6d3_COMPANION_VOLUME_PLAN.md`

## Requirement Status

- Shared helper in `awf.common.companions`: Complete.
- API schema validation uses the shared helper: Complete.
- Node runtime volume resolution uses the shared helper: Complete.
- Existing named-volume and repo-relative path behavior is preserved: Complete.
- Focused tests only during the agent phase: Complete.

## Evidence

Changed files:

- `src/awf/common/companions.py`
- `src/awf/api/schemas_companions.py`
- `src/awf/node/companion_services.py`
- `tests/unit/common/test_companions.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_companions.py -q`
  - Expected initial failure before implementation:
    `ImportError: cannot import name 'companion_volume_source_is_repo_relative'`.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_companions.py tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py -q`
  - Passed: 69 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/companions.py src/awf/api/schemas_companions.py src/awf/node/companion_services.py tests/unit/common/test_companions.py`
  - Passed.

Full AWF/GitHub validation, whole-repository test suites, and coverage gates
were not run in the agent phase; AWF owns those broad validation gates after
agent completion.

## Gaps

None.
