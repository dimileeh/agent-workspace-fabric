# Address PRRT_kwDOSJAM6s6GDqo8 Plan

## Problem Statement and Scope

The PR review thread reports that branch ruleset parsing treats a
`pull_request` rule with `allowed_merge_methods` containing only unknown method
names as an explicit empty merge-method constraint. That causes the monitor to
see no allowed methods instead of ignoring that rule's method list like an
omitted `allowed_merge_methods` field.

Scope is limited to `GitHubClient.fetch_branch_pull_request_allowed_merge_methods`
and focused unit coverage for that parser behavior.

## Requirements Checklist

- Add a regression test proving unknown-only `allowed_merge_methods` is treated
  as unconstrained.
- Preserve existing behavior for recognized methods and for intersections of
  recognized branch rules.
- Keep validation focused to the changed parser tests; AWF/GitHub own broad
  validation after agent completion.
- Commit the thread-specific fix locally without pushing.

## Implementation Steps

1. Add a failing unit test in the existing GitHub client parser test group.
2. Update branch ruleset parsing to skip method lists that normalize to no
   known methods.
3. Update the method docstring if needed so it matches the parser contract.
4. Run the focused unit test and a nearby parser test selection.
5. Write validation notes in `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GDqo8_VALIDATION.md`.
6. Stage only changed files and commit with a thread-specific conventional
   commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "allowed_merge_methods"`
  passes.
- Initial new-test-only run should fail before implementation when practical.
