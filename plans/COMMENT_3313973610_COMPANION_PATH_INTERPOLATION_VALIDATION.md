# Comment 3313973610 Companion Path Interpolation Validation

Plan reference:
`plans/COMMENT_3313973610_COMPANION_PATH_INTERPOLATION_PLAN.md`

## Requirement Status

- Reject Docker Compose interpolation in companion `build_context`,
  `dockerfile`, and `env_file` repo-relative paths: Complete.
- Reject Docker Compose interpolation in companion volume source paths:
  Complete.
- Reject Docker Compose interpolation in companion volume target paths:
  Complete.
- Preserve existing rejection of unsafe YAML path characters and invalid path
  shape: Complete.
- Keep validation evidence narrow and leave full AWF/GitHub validation to the
  post-agent phase: Complete.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `plans/COMMENT_3313973610_COMPANION_PATH_INTERPOLATION_PLAN.md`
- `plans/COMMENT_3313973610_COMPANION_PATH_INTERPOLATION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  before implementation: failed on the new companion path interpolation
  regression cases.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  after implementation: `50 passed in 0.55s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`:
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/schemas_companions.py`:
  passed.

Full AWF/GitHub validation was not run inside the agent phase because the
workspace contract reserves broad validation and merge gating for AWF after
agent completion.

## Remaining Gaps

None.
