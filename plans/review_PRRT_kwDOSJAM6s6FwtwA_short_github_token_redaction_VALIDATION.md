# Short GitHub Token Redaction Validation

Plan reference:
`review_PRRT_kwDOSJAM6s6FwtwA_short_github_token_redaction_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving shortened GitHub token-looking
  values are redacted by shared redactors, including first-run value and
  mapping-key redaction.
- Complete: Updated the shared GitHub token pattern so bare prefixes and short
  suffixes are redacted.
- Complete: Left existing redaction markers, token assignment redaction, and
  other provider patterns unchanged.
- Complete: Ran only focused validation. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/common/token_patterns.py`
- `tests/unit/common/test_token_patterns.py`
- `tests/unit/service/test_host_setup_rendering.py`
- `plans/review_PRRT_kwDOSJAM6s6FwtwA_short_github_token_redaction_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6FwtwA_short_github_token_redaction_VALIDATION.md`

TDD failure observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py -q`
  failed for `ghp_`, `ghp_a`, `gho_a`, and `github_pat_a`.

Focused checks after implementation:

- Pass: `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py -q`
  (`7 passed`)
- Pass: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q`
  (`18 passed`)
- Pass: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_truncated_pat_values -q`
  (`6 passed`)
- Pass: `uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py tests/unit/common/test_token_patterns.py tests/unit/service/test_host_setup_rendering.py`
  (`All checks passed!`)

No gaps remain for this review thread.
