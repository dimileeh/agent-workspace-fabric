# Short GitHub Token Redaction Plan

## Problem Statement and Scope

The shared known-token regex redacts GitHub tokens only when at least six
characters follow prefixes such as `ghp_` or `github_pat_`. Host setup config
validation rejects any raw value beginning with those prefixes, including
shortened rejected values such as `ghp_a`. First-run diagnostics and shared audit
or log redactors should not echo those shortened values.

Scope is limited to shared token recognition and focused regression coverage for
the reported PR review thread `PRRT_kwDOSJAM6s6FwtwA`.

## Requirements Checklist

- Add a regression test proving shortened GitHub token-looking values are
  redacted by shared redactors, including first-run value and mapping-key
  redaction.
- Update the shared GitHub token pattern so values with only the rejected prefix
  or a short suffix are redacted.
- Keep existing redaction markers, token assignment redaction, and other token
  provider patterns unchanged.
- Run only focused validation; full AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add a failing unit regression for `ghp_`, `ghp_a`, `gho_a`, and
   `github_pat_a` alongside the existing shared token-pattern tests.
2. Confirm the new regression fails against the current regex.
3. Relax only the GitHub alternatives in `KNOWN_TOKEN_PATTERN` to match the
   rejected prefix with any suffix length.
4. Extend the existing first-run rendering truncated PAT regression to include
   the shortened GitHub values named by the review thread.
5. Re-run the focused token-pattern, log-redaction, and first-run rendering
   tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_truncated_pat_values -q`
  must pass.
- Broad repository validation, coverage, and CI-equivalent gates are intentionally
  not run in-agent under the AWF workspace contract.
