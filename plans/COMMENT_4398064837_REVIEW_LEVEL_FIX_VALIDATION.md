# Review-Level Comment 4398064837 Fix Validation

Plan reference:
`plans/COMMENT_4398064837_REVIEW_LEVEL_FIX_PLAN.md`

## Requirement Status

- Pretty-print
  `docs/awf-plans/ws_847ac67f6aad4050aed1fda0.conformance.json`:
  Complete.
- Prefix the two affected validation-doc plan references with `plans/`:
  Complete.
- Add a regression test proving setup-time cancellation blocks PR monitor factory
  side effects: Complete.
- Recheck workspace status after handoff profile setup and before adapter/monitor
  factory construction: Complete.
- Leave `monitor_handoff_audit.py` unchanged if its workspace fallback is already
  present: Complete. Current code uses
  `source_head_sha or workspace.monitor_last_commit_sha`, and existing focused
  coverage confirms the fallback.
- Leave `pre_push_validation.py` unchanged if command-less returncode `127`
  handling is already present: Complete. Current code checks
  `ValidationResult.first_failure`, and existing focused coverage confirms the
  terminal toolchain-missing path.
- Record focused validation evidence only: Complete.

## Evidence

Files changed:

- `docs/awf-plans/ws_847ac67f6aad4050aed1fda0.conformance.json`
- `plans/COMMENT_3330714584_MONITOR_HANDOFF_OWNERSHIP_VALIDATION.md`
- `plans/COMMENT_3331327765_MONITOR_HANDOFF_FALLBACK_VALIDATION.md`
- `plans/COMMENT_4398064837_REVIEW_LEVEL_FIX_PLAN.md`
- `plans/COMMENT_4398064837_REVIEW_LEVEL_FIX_VALIDATION.md`
- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`

Focused checks:

- Red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k setup_status_recheck`
  - Expected failure before implementation: `factory_calls == ['called']`.
- Final focused behavior tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k "setup_status_recheck or setup_before_monitor or monitor_factory_none"`
  - Pass: `3 passed, 18 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
  - Pass: `All checks passed!`
- JSON parse:
  `python -m json.tool docs/awf-plans/ws_847ac67f6aad4050aed1fda0.conformance.json >/tmp/comment_4398064837_conformance.json`
  - Pass.
- Existing audit fallback regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py -q -k defaults_source_head_sha_from_workspace`
  - Pass: `1 passed, 32 deselected`.
- Existing command-less toolchain regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k commandless_toolchain_missing`
  - Pass: `1 passed, 24 deselected`.

Full AWF/GitHub validation was not run locally because the workspace contract
assigns broad validation, provenance, and merge gating to AWF after agent
completion.

## Remaining Gaps

None.
