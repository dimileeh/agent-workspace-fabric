# PR 303 Supported Generator CI Validation

Plan reference: `plans/PR_303_SUPPORTED_GENERATOR_CI_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Reproduce the reported pytest failure before changing code. | Complete | `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q` failed with extra item `generate_install_manifest.py`. |
| Do not disable, skip, or weaken the cleanup guard. | Complete | `tests/unit/docs/test_api_surface_cleanup_docs.py` still asserts exact equality against an explicit supported script set. |
| Keep `scripts/` restricted to explicitly supported generator scripts. | Complete | The allowlist now names only `generate_install_manifest.py`, `generate_openapi.py`, and `generate_reason_catalog.py`; unrelated files still fail the test. |
| Recognize the install manifest generator as supported. | Complete | `SUPPORTED_GENERATOR_SCRIPTS` includes `generate_install_manifest.py`, matching the release manifest tooling already referenced by docs and focused tests. |
| Run focused validation only. | Complete | Only the reported pytest node and focused ruff check below were run. Full AWF/GitHub validation remains managed by AWF after agent completion. |
| Commit the local fix without pushing or switching branches. | Complete | Work was committed locally on the existing branch; no push or branch switch was performed. |

## Evidence

- Changed files:
  - `tests/unit/docs/test_api_surface_cleanup_docs.py`
  - `plans/PR_303_SUPPORTED_GENERATOR_CI_PLAN.md`
  - `plans/PR_303_SUPPORTED_GENERATOR_CI_VALIDATION.md`

- Focused repro before fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q
```

Result before implementation: failed with extra script
`generate_install_manifest.py`.

- Focused pytest after fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q
```

Result: passed, `1 passed in 0.38s`.

- Focused lint after fix:

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_api_surface_cleanup_docs.py
```

Result: passed, `All checks passed!`.

## Remaining Gaps

None. Broad full-coverage and CI-equivalent validation were not run locally per
the AWF workspace contract; AWF/GitHub own those checks after this agent phase.
