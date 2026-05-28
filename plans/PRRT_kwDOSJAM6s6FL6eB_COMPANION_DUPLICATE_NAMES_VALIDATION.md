# PRRT_kwDOSJAM6s6FL6eB Companion Duplicate Names Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FL6eB_COMPANION_DUPLICATE_NAMES_PLAN.md`

## Requirement Status

- Detect duplicate `companion.spec.name` values before dependency validation and
  cycle graph construction: Complete.
- Raise `ProfileResolutionError` with reason code
  `COMPANION_SERVICE_NAME_COLLISION` for duplicate companion names: Complete.
- Include the duplicate companion service name(s) in the error message:
  Complete.
- Preserve existing profile service collision behavior and reason code:
  Complete.
- Validate with a targeted unit test command for
  `tests/unit/node/test_companion_services.py`: Complete.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`
- `plans/PRRT_kwDOSJAM6s6FL6eB_COMPANION_DUPLICATE_NAMES_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FL6eB_COMPANION_DUPLICATE_NAMES_VALIDATION.md`

Focused checks run:

- Red step:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_validate_companion_service_graph_rejects_duplicate_companion_names -q`
  failed before implementation with `Failed: DID NOT RAISE
  <class 'awf.profiles.resolver.ProfileResolutionError'>`.
- Green step:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_validate_companion_service_graph_rejects_duplicate_companion_names -q`
  passed with `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  passed with `14 passed`.
- `uv run --python 3.12 --extra dev ruff format src/awf/node/companion_services.py tests/unit/node/test_companion_services.py`
  reformatted `src/awf/node/companion_services.py`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py`
  passed.

Broad AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after agent completion.

## Gaps

None.
