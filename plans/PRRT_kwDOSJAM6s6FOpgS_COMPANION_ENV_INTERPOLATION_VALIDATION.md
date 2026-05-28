# Companion Environment Interpolation Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FOpgS_COMPANION_ENV_INTERPOLATION_PLAN.md`

## Requirement Status

- Reject companion-supplied environment values containing Docker Compose
  interpolation syntax: Complete.
- Preserve ordinary literal companion environment values, including values with
  non-interpolation dollar characters: Complete.
- Add a regression test proving `${GITHUB_TOKEN}` is rejected by the public
  workspace create contract: Complete.
- Keep validation focused; broad AWF/GitHub validation remains managed by AWF
  after agent completion: Complete.

## Evidence

Changed files:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`

Focused commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Before implementation: failed because `${GITHUB_TOKEN}` was accepted.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q -k "companion"`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Reformatted `src/awf/api/schemas_companions.py`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Passed.

Full AWF/GitHub validation and CI-equivalent gates were not run in the agent
phase per the AWF workspace contract.
