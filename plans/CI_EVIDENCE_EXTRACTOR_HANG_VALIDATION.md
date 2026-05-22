# CI Evidence Extractor Hang Validation

Plan reference: `plans/CI_EVIDENCE_EXTRACTOR_HANG_PLAN.md`

## Requirement Status

- Replace nested pytest node-id regex path with bounded, linear parsing:
  Complete. `src/awf/runtime/ci_failure_evidence.py` now scans around `.py::`
  anchors and validates node boundaries instead of running nested regexes across
  every CI line.
- Add regression coverage for noisy/malformed CI lines:
  Complete. `tests/unit/runtime/test_ci_failure_evidence.py` covers a long
  malformed pytest-like line followed by a valid failed node.
- Preserve normal, nested, and parameterized pytest node extraction:
  Complete. Existing GitHub-client failing-check tests still pass, and new
  coverage verifies bracketed parameter ids with whitespace and shell-like text.
- Verify against captured PR #480 failed-check log:
  Complete. Extraction against `/tmp/awf-pr480-failed.log` completed in about
  `0.023s` for a 221,981-byte log.
- Rebuild/restart local AWF worker and remonitor the stuck workspace:
  Complete for the root fix. The worker was rebuilt/restarted, CPU returned to
  normal, `ws_206ffcf5c60a46ccad56fbe0` emitted `monitor.action=AddressComments`,
  and Codex Spark began processing PR #480 review threads.

## Evidence

- Focused tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py::TestFetchFailingCheckLogs -q`
  passed: `40 passed`.
- Lint/format/type checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py`
  passed.
  `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py`
  passed.
  `uv run --python 3.12 --extra dev mypy src/awf/runtime/ci_failure_evidence.py`
  passed.
- Captured-log reproduction:
  `extract_ci_failure_evidence()` completed in `0.0232s` against the PR #480
  failed-check log.
- Live AWF recovery:
  Worker logs show `monitor.action action=AddressComments ... pr_number=480`
  followed by `agent.run.start agent=codex model=gpt-5.3-codex-spark`.
  The workspace worktree advanced with local fix commits including `f2b2a0c0`
  and `cd5b27f6`.

## Notes

The first worker restart surfaced a stale-active-execution recovery event for
the previously wedged monitor, but the explicit remonitor operation recovered
the workspace and the monitor loop is now processing review threads.
