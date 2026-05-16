# PRRT_kwDOSJAM6s6CilY1 CI Evidence Profile Fallback Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6CilY1` reports that pytest node IDs without a
trusted GitHub Actions `Run ... pytest` line produce no focused repro command.
The existing branch intentionally removed a hardcoded AWF pytest fallback because
`GitHubClient.fetch_failing_check_logs` is used for generic monitored
repositories. The fix must restore focused fallback commands only when AWF has a
trusted workspace validation/test command to derive the pytest prefix from.

Scope is limited to CI failure evidence extraction, GitHub check-log fetching,
PR monitor status refresh plumbing, and focused tests.

## Requirements Checklist

- Preserve the default safety behavior: node-ID-only logs do not synthesize a
  command unless trusted fallback commands are supplied.
- Add fallback repro command creation from trusted workspace validation/test
  commands when pytest node IDs exist and no CI run-step pytest prefix was
  parsed.
- Preserve extracted CI pytest command precedence over fallback commands.
- Keep fallback commands bounded to the existing repro node limit and quote node
  IDs with the existing command construction path.
- Pass workspace `test_commands` from the PR monitor into failing-check log
  extraction.
- Avoid hardcoded AWF-specific command prefixes in generic CI evidence or GitHub
  client code.

## Implementation Steps

1. Add/update focused regression tests for profile-derived fallback behavior in
   `tests/unit/runtime/test_ci_failure_evidence.py`.
2. Add GitHub client coverage proving fallback commands are passed through only
   when supplied and no run-step pytest command is present.
3. Add PR monitor runner coverage proving workspace `test_commands` are supplied
   to failing-check log fetching.
4. Update CI evidence extraction and GitHub client/runner plumbing narrowly.
5. Run focused tests and lint for the touched files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_runner.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/ci_failure_evidence.py src/awf/common/github_client.py src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_runner.py
```

Pass criteria: all commands succeed, and the validation document records every
checklist item as complete or explains a concrete defer reason.
