# Comment 4571563982 Redaction Cleanup Plan

## Problem Statement and Scope

Address the remaining PR review feedback from `issue:4571563982` around first-run and shared redaction hardening:

- Make the audit/runtime choice to redact truncated GitHub/GitLab/Slack token prefixes explicit at the call sites while preserving the existing regression-backed no-leak behavior.
- Stop `render_first_run_json()` cleanup from mutating the `model_dump(mode="python")` dictionary returned by Pydantic.
- Reduce provider-ref key suffix sensitivity checks so they do not run the full audit/token/provider redaction pipeline for each suffix.

Scope is limited to the shared token pattern helper, audit/runtime redactor call-site declarations, first-run rendering cleanup, focused unit coverage, this plan, and the matching validation document.

## Requirements Checklist

- Preserve existing tests that require truncated rejected token values to be redacted by audit, runtime log, and first-run redactors.
- Add call-site-visible intent for audit/runtime truncated token redaction.
- Add a regression test proving first-run JSON cleanup does not mutate the raw dump object returned by `model_dump()`.
- Add a regression test proving provider-ref key suffix sensitivity short-circuits without invoking the audit redaction pipeline for known token/provider-ref suffixes.
- Keep rendered first-run JSON shape unchanged for existing callers.
- Run only targeted tests for the changed redaction/rendering behavior; broad AWF/GitHub validation remains managed after agent completion.
- Commit the local fix on the current AWF-managed branch without pushing or switching branches.

## Implementation Steps

1. Add focused failing tests for raw dump immutability and suffix redaction short-circuiting.
2. Extend `compile_known_token_re()` with a named option that makes the truncated-token matching policy explicit at each call site without changing the current pattern semantics.
3. Update audit and runtime log redactors to pass the explicit truncated-token option and document the tradeoff locally.
4. Refactor `render_first_run_json()` cleanup into a helper that builds fresh wrapper dictionaries before removing empty optional collections.
5. Replace `_is_sensitive_provider_ref_key_suffix()` with direct full-match checks for already-redacted, known-token, and provider-ref suffixes.
6. Run the focused tests covering the new regressions and the existing shared token-pattern contract.
7. Record verification in `plans/COMMENT_4571563982_REDACTION_CLEANUP_VALIDATION.md`.
8. Stage only changed files and commit with a conventional commit message for comment `4571563982`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_json_cleanup_does_not_mutate_model_dump_result tests/unit/service/test_host_setup_rendering.py::test_provider_ref_key_suffix_sensitivity_short_circuits_known_sensitive_suffixes -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py tests/unit/common/test_token_patterns.py tests/unit/common/test_audit.py tests/unit/runtime/test_log_redaction.py -q`

Pass criteria: focused tests pass locally and the existing shared truncated-token redaction contract remains green. Full AWF/GitHub validation, broad lint/type checks, full repository tests, frontend builds, and coverage gates are intentionally left to AWF after agent completion per workspace contract.
