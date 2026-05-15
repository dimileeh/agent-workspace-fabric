# Callback Allowed Host Port Normalization Plan

## Problem Statement And Scope

An unresolved PR review thread reports that `callbacks_allowed_hosts` accepts
entries such as `example.com:8080`, but callback validation compares the
normalized allowlist against `urlsplit(target_url).hostname`, which excludes the
port. This can reject callback URLs whose host was intended to be allowlisted.

Scope is limited to callback allowed-host normalization and a focused regression
test.

## Requirements Checklist

- Add a regression test proving allowlist entries with port suffixes normalize
  to bare hostnames.
- Preserve existing normalization for comma-separated environment values,
  whitespace, trailing dots, and lowercase hostnames.
- Implement the smallest code change needed in `src/awf/common/config.py`.
- Run the narrow unit test that covers the change.
- Commit the thread-specific fix locally without pushing.

## Implementation Steps

1. Add or update a focused unit test in `tests/unit/common/test_common_polish.py`.
2. Run the new test and confirm it fails before implementation when practical.
3. Update the `callbacks_allowed_hosts` validator to strip port suffixes
   consistently with `urlsplit().hostname`.
4. Re-run the focused unit tests.
5. Record validation evidence in the matching validation document.
