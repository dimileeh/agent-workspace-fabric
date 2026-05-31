# PRRT_kwDOSJAM6s6F8MC0 Provider Inference Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F8MC0_PROVIDER_INFERENCE_PLAN.md`

## Requirement Status

- Add a regression test showing Cursor-specific markers take precedence over
  broad Google markers: Complete.
- Keep existing Google inference behavior intact: Complete.
- Make the smallest implementation change needed in
  `src/awf/adapters/provider_failures.py`: Complete.
- Run focused local validation only; full AWF/GitHub validation is managed after
  agent completion: Complete.

## Evidence

Files changed:

- `src/awf/adapters/provider_failures.py`
- `tests/unit/adapters/test_provider_failures.py`
- `plans/PRRT_kwDOSJAM6s6F8MC0_PROVIDER_INFERENCE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F8MC0_PROVIDER_INFERENCE_VALIDATION.md`

Focused validation:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py::test_cursor_provider_inference_takes_precedence_over_google_markers -q`
  failed because `infer_provider()` returned `google` for mixed Cursor and
  Google quota output.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py::test_cursor_provider_inference_takes_precedence_over_google_markers -q`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py -q`
  passed with 14 tests.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/adapters/provider_failures.py tests/unit/adapters/test_provider_failures.py`
  passed.

Full AWF/GitHub validation was not run inside the agent phase per the workspace
contract; AWF owns broad validation after agent completion.
