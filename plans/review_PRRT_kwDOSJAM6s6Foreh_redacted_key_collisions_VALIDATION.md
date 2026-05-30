# Review PRRT_kwDOSJAM6s6Foreh Redacted Key Collisions Validation

Plan reference: `plans/review_PRRT_kwDOSJAM6s6Foreh_redacted_key_collisions_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving colliding redacted mapping keys
  retain every redacted entry instead of overwriting earlier values.
- Complete: Raw token-like keys remain absent from rendered JSON and pretty
  output.
- Complete: Existing provider-reference key redaction behavior is preserved.
- Complete: Validation stayed focused. Full AWF/GitHub validation was not run
  inside the agent phase because AWF owns broad validation after completion.

## Evidence

Files changed:

- `src/awf/host_setup/rendering.py`
- `tests/unit/service/test_host_setup_rendering.py`
- `plans/review_PRRT_kwDOSJAM6s6Foreh_redacted_key_collisions_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6Foreh_redacted_key_collisions_VALIDATION.md`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_preserves_colliding_redacted_mapping_keys -q`
  - Initial failure before the renderer change: the second redacted key
    overwrote the first value under `[redacted]`.
  - Pass after the renderer change: `1 passed in 0.42s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_token_and_provider_ref_detail_keys tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_preserves_colliding_redacted_mapping_keys tests/unit/service/test_host_setup_rendering.py::test_provider_ref_key_redaction_requires_explicit_ref_key tests/unit/service/test_host_setup_rendering.py::test_provider_ref_redaction_preserves_tuple_container_type tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_preserves_tuple_container_type -q`
  - Pass: `5 passed in 0.44s`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Pass: `2 files already formatted`
- `uv run --python 3.12 --extra dev mypy src/awf/host_setup/rendering.py`
  - Pass: `Success: no issues found in 1 source file`
