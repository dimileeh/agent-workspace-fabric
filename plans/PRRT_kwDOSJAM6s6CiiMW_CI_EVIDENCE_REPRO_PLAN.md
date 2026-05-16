# PRRT_kwDOSJAM6s6CiiMW CI Evidence Repro Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CiiMW` reports that CI failure evidence extraction
hardcodes this repository's `uv run --python 3.12 --extra dev pytest` command when
pytest node IDs are present but no trusted pytest run command was parsed from the
GitHub Actions log. `GitHubClient.fetch_failing_check_logs` applies this extractor
to generic monitored repositories, so the hardcoded AWF command can mislead repair
agents for projects using tox, poetry, Docker, or plain pytest.

Scope is limited to CI failure evidence extraction and its focused unit coverage.
The extractor should continue to expose pytest node IDs and build focused commands
from trusted CI run-step commands.

## Requirements Checklist

- Keep extracting and redacting pytest node IDs, assertion snippets, error summaries,
  and trusted failing commands from CI logs.
- Do not emit a hardcoded AWF pytest repro command when no compatible pytest prefix
  is parsed from a trusted CI run-step line.
- Continue emitting bounded, shell-quoted focused pytest commands when a compatible
  pytest prefix is parsed from the CI log.
- Preserve the guard that untrusted printed pytest-looking lines are not promoted to
  executable repro commands.
- Add/update regression coverage before implementation, including the review-reported
  no-command case.

## Implementation Steps

1. Update focused tests to assert absent suggested repro commands when only pytest
   node IDs are available.
2. Keep or add tests proving bounded/quoted command generation still works with a
   trusted pytest run-step prefix.
3. Remove the AWF-specific fallback from `src/awf/runtime/ci_failure_evidence.py`.
4. Run the focused tests, then run narrow lint for touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py -q`
  should pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py`
  should pass.
