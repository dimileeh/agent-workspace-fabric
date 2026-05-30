# PRRT_kwDOSJAM6s6FuFA0 Shared Token Redaction Plan

## Problem Statement And Scope

The unresolved review thread reports that token-shaped secret regexes are copied
between first-run rendering, audit payload redaction, and operator log
redaction. The copies have already drifted, so adding a new token prefix can
leave one security-sensitive redaction path behind.

Scope is limited to centralizing the shared known-token pattern and proving the
three redaction callers use it. No GitHub thread resolution, push, branch
switch, protected configuration changes, or broad AWF/GitHub validation are in
scope.

## Requirements Checklist

- Add a focused regression proving audit, log redaction, and first-run rendering
  use the same shared known-token regex pattern.
- Add focused log-redaction coverage for token prefixes currently covered by
  the shared audit/first-run pattern but missing from the general log redactor.
- Preserve first-run case-insensitive token redaction.
- Implement the smallest shared-pattern extraction needed to remove duplicated
  token alternation lists from the affected modules.
- Run targeted validation only for the changed behavior and touched source
  files.
- Record validation evidence in
  `plans/PRRT_kwDOSJAM6s6FuFA0_SHARED_TOKEN_REDACTION_VALIDATION.md`, noting
  that full AWF/GitHub validation is managed after agent completion.

## Implementation Steps

1. Add a focused regression test for shared known-token pattern use and extend
   the existing log redaction test with GitLab and Slack token-shaped values.
2. Run the focused tests to confirm they fail before implementation.
3. Introduce a common token-pattern helper in `awf.common`.
4. Update audit redaction, operator log redaction, and first-run rendering to
   compile their known-token regexes from the shared helper.
5. Re-run the focused tests and focused lint/type checks.
6. Write the validation document and commit the scoped changes locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py::test_redact_secrets_preserves_context_while_removing_known_secret_bodies -q`

Pass criteria before implementation: the new regression fails because no shared
pattern helper exists and/or log redaction misses the added prefixes.

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py::test_redact_secrets_preserves_context_while_removing_known_secret_bodies tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_tokens_provider_refs_and_sensitive_keys tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_case_variant_token_prefixes tests/unit/common/test_audit.py::test_redact_audit_value_recursively_redacts_secrets_without_losing_token_usage -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py src/awf/common/audit.py src/awf/common/redaction.py src/awf/host_setup/rendering.py tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py`
- `uv run --python 3.12 --extra dev mypy src/awf/common/token_patterns.py src/awf/common/audit.py src/awf/common/redaction.py src/awf/host_setup/rendering.py`

Pass criteria after implementation: focused tests and focused checks pass. Full
AWF/GitHub validation remains managed by AWF after agent completion.
