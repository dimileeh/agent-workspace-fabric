# PRRT_kwDOSJAM6s6FPaGq Companion Volume Source Colon Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FPaGq_COMPANION_VOLUME_SOURCE_COLON_PLAN.md`

## Result

- Reject `:` in repo-relative companion volume sources: Complete.
- Preserve existing valid repo-relative and named-volume source behavior:
  Complete.
- Add focused API schema regression coverage for the reported case: Complete.

## Files Changed

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `plans/PRRT_kwDOSJAM6s6FPaGq_COMPANION_VOLUME_SOURCE_COLON_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FPaGq_COMPANION_VOLUME_SOURCE_COLON_VALIDATION.md`

## Evidence

Initial failing regression before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_repo_relative_volume_sources_with_colons -q
```

Result: failed with `DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>`.

Focused checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_repo_relative_volume_sources_with_colons -q
```

Result: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_repo_relative_volume_sources_with_colons -q
```

Result: `28 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/api/schemas_companions.py
```

Result: `Success: no issues found in 1 source file`.

Full AWF/GitHub validation is managed by AWF after agent completion.
