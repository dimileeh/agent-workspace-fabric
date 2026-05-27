# PRRT_kwDOSJAM6s6FKZcb Companion Null Sequences Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FKZcb_COMPANION_NULL_SEQUENCES_PLAN.md`

## Requirement Status

- Add a regression test showing explicit null `depends_on`, `ports`, and
  `volumes` normalize to empty tuples: Complete.
- Preserve existing validation for malformed non-null sequence entries:
  Complete.
- Change only companion normalization code needed for this thread: Complete.
- Run focused unit tests for the changed behavior: Complete.
- Do not run broad AWF/GitHub-owned validation: Complete. Full AWF/GitHub
  validation is intentionally left to AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`

Focused checks:

- Pre-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_treats_null_optional_sequences_as_empty -q`
  failed with `TypeError: 'NoneType' object is not iterable`.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_treats_null_optional_sequences_as_empty -q`
  passed.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  passed.
- Post-fix:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py`
  passed.
- Post-fix:
  `uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py`
  passed.

## Gaps

No known gaps for this review thread.
