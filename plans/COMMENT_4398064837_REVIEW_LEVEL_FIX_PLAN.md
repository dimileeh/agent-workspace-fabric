# Review-Level Comment 4398064837 Fix Plan

## Problem Statement

CodeRabbit review-level comment `4398064837` summarizes several inline findings
for PR 348. Current code must be verified locally, only still-valid items should
be fixed, and broad AWF/GitHub validation must remain owned by AWF after this
agent completes.

## Scope

In scope:

- Fix maintainability-only formatting/path issues in existing plan artifacts.
- Add the missing stale-status guard after `sync_feature_pr` monitor handoff
  profile setup and before monitor factory construction.
- Preserve existing behavior for already-fixed or stale findings.
- Add focused regression coverage for the behavior change.

Out of scope:

- Rewriting existing review-thread fixes.
- Running full repository tests, full coverage, frontend builds, or CI-equivalent
  validation.
- Pushing or changing branches.

## Requirements Checklist

- [ ] Pretty-print
  `docs/awf-plans/ws_847ac67f6aad4050aed1fda0.conformance.json` with a trailing
  newline.
- [ ] Prefix the plan reference paths in the two affected validation documents
  with `plans/`.
- [ ] Add a regression test proving a workspace cancelled during monitor handoff
  setup does not call the PR monitor factory.
- [ ] Implement a running-status recheck after handoff profile setup and before
  adapter/monitor factory creation.
- [ ] Leave `monitor_handoff_audit.py` unchanged if its workspace fallback is
  already present.
- [ ] Leave `pre_push_validation.py` unchanged if command-less returncode `127`
  handling is already present.
- [ ] Record focused validation evidence only.

## Implementation Steps

1. Add the failing regression test in
   `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`.
2. Run the narrow red test and capture the expected failure.
3. Update `src/awf/control/executor/monitor_handoff.py` so setup success is
   followed by an executor status recheck before monitor factory side effects.
4. Apply the JSON/path formatting fixes.
5. Run focused tests and lint for the touched Python files plus a JSON parse
   check for the conformance record.
6. Create a validation document against this plan.

## Verification Commands

- Red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k setup_status_recheck`
- Focused behavior tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k "setup_status_recheck or setup_before_monitor or monitor_factory_none"`
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- JSON parse:
  `python -m json.tool docs/awf-plans/ws_847ac67f6aad4050aed1fda0.conformance.json >/tmp/comment_4398064837_conformance.json`

Full AWF/GitHub validation is intentionally not run locally; AWF owns broad
validation, provenance, and merge gating after agent completion.
