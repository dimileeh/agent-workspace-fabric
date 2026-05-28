# Review 4567320760 Maintainability Plan

## Problem Statement And Scope

Address the remaining maintainability suggestions from PR review comment
`issue:4567320760` in the host setup config layer. Scope is limited to
`src/awf/host_setup/config.py`, focused regression tests in
`tests/unit/service/test_host_setup_config.py`, and the required plan/validation
artifacts.

## Requirements Checklist

- Allow `_config_corrupt_error` to omit `details`, matching
  `HostSetupConfigError(details=None)`.
- Document that `HostSetupConfig.work_dir` may contain `~` and must be expanded
  by callers before filesystem use.
- Avoid constructing YAML mapping key nodes twice in the duplicate-key-rejecting
  loader.
- Preserve existing duplicate-key, secret-scan, and error-sanitization behavior.
- Run only focused local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add focused tests that fail on the current helper signature, missing
   `work_dir` field description, and duplicate key-node construction.
2. Update `config.py` with the minimal implementation changes.
3. Run the targeted host setup config tests that cover the changed behavior.
4. Record requirement-by-requirement validation in
   `plans/REVIEW_4567320760_MAINTAINABILITY_VALIDATION.md`.
5. Stage only changed files and commit locally with a conventional review-fix
   message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  must pass.
- No full coverage gate, whole-repository suite, frontend build, push, rebase,
  or branch switch will be run in the agent phase.
