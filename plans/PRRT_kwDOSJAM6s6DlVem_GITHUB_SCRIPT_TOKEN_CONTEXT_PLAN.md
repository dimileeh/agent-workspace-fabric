# PRRT_kwDOSJAM6s6DlVem GitHub Script Token Context Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DlVem` reports that the
`actions/github-script` comment/notify safety check admits scripts that read
token context values such as `github.token` or `context.token` before calling an
allowed comment API. That would let an unowned protected workflow edit exfiltrate
the token through a PR comment while passing the informational-step allowlist.

Scope is limited to the `actions/github-script` safety predicate in
`src/awf/control/quality_gates.py`, focused unit coverage in
`tests/unit/control/test_quality_gates.py`, and this plan/validation pair.

## Requirements Checklist

- Add regression coverage proving a comment-labeled `actions/github-script`
  step that reads token context values is blocked.
- Keep existing safe GitHub comment scripts that use non-sensitive context
  fields admitted.
- Keep existing blocks for unsafe APIs and process access intact.
- Commit only the files changed for this review thread.

## Implementation Steps

1. Add failing regression cases for `github.token` and `context.token` access in
   an otherwise allowed GitHub comment script.
2. Run the focused regression and confirm it fails before the production fix.
3. Update the blocked-access predicate to reject token property reads from the
   GitHub script context while preserving safe `context.repo`, `context.issue`,
   and `context.sha` use.
4. Re-run focused quality-gate tests and static checks for the touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  fails before the fix for the new token-context regression and passes after the
  fix.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passes.
