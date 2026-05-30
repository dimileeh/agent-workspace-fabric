# Comment 3322540349 GitLab Token Redaction Plan

## Problem Statement And Scope

The unresolved PR review thread reports that first-run setup/start rendering can leak GitLab-style personal access tokens such as `glpat-...` in JSON and pretty output. The renderer already redacts provider references and delegates token-shaped text redaction to `awf.common.audit`, but the delegated token pattern does not cover the `glpat-` prefix that host setup config treats as a secret value.

Scope is limited to first-run rendering redaction behavior and the shared audit token recognizer it delegates to. No GitHub thread resolution, push, branch switch, broad validation, or protected config changes are in scope.

## Requirements Checklist

- Add a focused regression proving `render_first_run_json()` and `render_first_run_pretty()` do not expose `glpat-...` values embedded in first-run summaries/details.
- Preserve existing first-run provider-ref redaction and sensitive-key redaction behavior.
- Implement the smallest redaction change needed so the regression passes.
- Run targeted validation only for the changed behavior.
- Record validation evidence in `plans/COMMENT_3322540349_GITLAB_TOKEN_REDACTION_VALIDATION.md`, noting that broad AWF/GitHub validation is handled after agent completion.

## Implementation Steps

1. Extend the existing first-run rendering redaction test with a GitLab PAT-shaped value.
2. Run the focused test to confirm the new regression fails before implementation.
3. Add `glpat-` token recognition to the audit redactor used by first-run rendering.
4. Re-run the focused renderer test.
5. Add the validation document and commit the scoped changes locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_tokens_provider_refs_and_sensitive_keys -q`

Pass criteria: the focused test fails before the implementation change because the GitLab token remains visible, and passes after the implementation change with no raw `glpat-...` value in rendered JSON or pretty output.
