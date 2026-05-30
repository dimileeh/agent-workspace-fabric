# Comment 4571563982 Review-Level Redaction and Sort Plan

## Problem Statement and Scope

PR review comment `issue:4571563982` includes two actionable findings:

- Pretty rendering sorts redacted collision keys lexicographically, so suffixes
  such as `[redacted]#10` can appear before `[redacted]#2`.
- `awf.common.redaction.redact_secrets` recognizes fewer assignment-style
  secret keys than audit redaction, leaving generic token, password, secret,
  API key, and access key assignments under-redacted in operator log text.

Scope is limited to first-run pretty rendering key ordering, shared/common
text redaction assignment coverage, focused regression tests, and this plan /
validation record.

## Requirements Checklist

- Add a regression test showing pretty details render redacted suffixes in
  numeric order once there are ten or more collisions.
- Add regression coverage showing `redact_secrets` redacts assignment keys
  already covered by audit redaction.
- Implement a numeric-aware key sort for pretty mapping output without changing
  JSON payload keys or collision preservation.
- Align common text redaction assignment matching with audit assignment
  matching.
- Run targeted tests only; full AWF/GitHub validation remains managed by AWF
  after agent completion.
- Commit the scoped changes locally without switching branches or pushing.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_host_setup_rendering.py` and
   `tests/unit/runtime/test_log_redaction.py`.
2. Confirm the targeted tests fail before implementation when practical.
3. Add a numeric suffix sort helper in `src/awf/host_setup/rendering.py`.
4. Share or otherwise align the token assignment regex used by
   `src/awf/common/audit.py` and `src/awf/common/redaction.py`.
5. Re-run the focused tests that cover the changed behavior.
6. Create `plans/COMMENT_4571563982_REVIEW_LEVEL_REDACTION_SORT_VALIDATION.md`
   with requirement-by-requirement evidence.
7. Stage only changed files and create one local conventional commit for the
   review comment.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_sorts_redacted_collision_suffixes_numerically tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values -q`
  - Passes after implementation; fails before implementation for the added
    cases.
- Full repository validation, coverage gates, frontend builds, and CI-equivalent
  checks are intentionally not run during the agent phase per the AWF workspace
  contract.
