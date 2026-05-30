# PRRT_kwDOSJAM6s6FuFA0 Shared Token Redaction Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FuFA0_SHARED_TOKEN_REDACTION_PLAN.md`

## Requirement Status

- Complete: Added a focused regression proving audit, log redaction, and
  first-run rendering compile known-token redaction from
  `awf.common.token_patterns.KNOWN_TOKEN_PATTERN`.
- Complete: Added log-redaction coverage for `glpat-...` and `xoxb-...`
  token-shaped values that were already covered by the audit/first-run pattern.
- Complete: Preserved first-run case-insensitive token redaction by compiling
  the shared pattern with `ignorecase=True` for rendering.
- Complete: Removed duplicated known-token alternation lists from audit, log
  redaction, and first-run rendering.
- Complete: Ran targeted validation only. Full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/common/token_patterns.py`
- `src/awf/common/audit.py`
- `src/awf/common/redaction.py`
- `src/awf/host_setup/rendering.py`
- `tests/unit/common/test_token_patterns.py`
- `tests/unit/runtime/test_log_redaction.py`
- `plans/PRRT_kwDOSJAM6s6FuFA0_SHARED_TOKEN_REDACTION_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FuFA0_SHARED_TOKEN_REDACTION_VALIDATION.md`

Commands run:

- Pre-fix expected failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py::test_redact_secrets_preserves_context_while_removing_known_secret_bodies -q`
  - Result: failed during collection because `awf.common.token_patterns` did not
    exist yet.
- Post-fix targeted regressions:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py::test_redact_secrets_preserves_context_while_removing_known_secret_bodies tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_tokens_provider_refs_and_sensitive_keys tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_case_variant_token_prefixes tests/unit/common/test_audit.py::test_redact_audit_value_recursively_redacts_secrets_without_losing_token_usage -q`
  - Result: passed, `5 passed in 0.50s`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py src/awf/common/audit.py src/awf/common/redaction.py src/awf/host_setup/rendering.py tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py`
  - Result: passed, `All checks passed!`.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/common/token_patterns.py src/awf/common/audit.py src/awf/common/redaction.py src/awf/host_setup/rendering.py`
  - Result: passed, `Success: no issues found in 4 source files`.
- Duplication spot-check:
  `rg -n "gh\\[apousr\\]|github_pat_\\[A-Za-z0-9_|glpat-\\[A-Za-z0-9_|xox\\[baprs\\]" src/awf/common src/awf/host_setup/rendering.py`
  - Result: the known-token alternation appears only in
    `src/awf/common/token_patterns.py`.

## Remaining Gaps

None for the planned scope.
