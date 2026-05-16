# GitHub CI Failure Evidence Repair Validation

Plan reference: `plans/github_ci_failure_evidence_repair_PLAN.md`

## Requirement Status

- Complete: Extract structured evidence from `gh run view --log-failed` output:
  failing commands, pytest node IDs, assertion/error snippets, error summaries,
  suggested focused repro commands, and unavailable/partial-log warnings.
- Complete: Redact secrets before evidence is truncated or rendered into
  prompts.
- Complete: Keep extraction provider-neutral and avoid hardcoding AWF check
  names such as `python-full-coverage`.
- Complete: Preserve current missing-log fallback behavior.
- Complete: Update CI repair prompts so focused repro evidence appears before
  raw logs.
- Complete: Explicitly tell agents to run focused repro commands first and not
  run broad/full coverage merely to discover an already-known CI failure.
- Complete: Keep all external CI log material quoted as untrusted evidence.
- Complete: Record redacted structured evidence summaries in PR-monitor
  operation payloads.
- Complete: Add TDD regression coverage for single-test, multi-test, non-test,
  unavailable-log, secret-redaction, and provider-neutral check cases.
- Complete: Address PR review follow-ups:
  - pytest node IDs with spaces or shell metacharacters are preserved and
    shell-quoted in focused repro commands;
  - empty metadata-only summaries are not rendered as separate evidence blocks;
  - unavailable-log warnings are rendered once;
  - `GitHubClient` computes check names once and passes raw logs into the
    extractor so `extract_ci_failure_evidence` owns evidence redaction;
  - the CI wall-clock grace-marker assertion tolerates scheduler delay while
    preserving the intended lower bound;
  - missing GitHub run IDs are omitted instead of rendered as `run_id: None`;
  - fallback non-`FAILED` pytest evidence preserves parametrized node IDs with
    spaces before shell-quoting focused repro commands;
  - pytest parameter IDs containing ` - ` are preserved rather than truncated at
    the failure-summary delimiter;
  - command-like CI log lines remain evidence only and are not promoted into
    executable focused repro commands unless derived from pytest node IDs;
  - missing-log details are rendered inside untrusted evidence blocks;
  - pytest node-ID dedupe preserves significant whitespace in parametrized IDs;
  - focused pytest repro commands are derived from trusted GitHub Run-step
    pytest commands, not hard-coded to AWF's own `uv` workflow, and untrusted
    printed command-like output is not promoted;
  - exact pytest selectors are extracted from full redacted CI lines before
    display truncation is applied, so long parametrized test IDs remain
    actionable while rendered snippets stay bounded;
  - nested pytest node paths such as `pkg/tests/test_api.py::test_x` preserve
    their full relative path instead of being truncated to `tests/...`;
  - PR-monitor transient-CI rerun classification consults structured test
    evidence before log-tail transient markers, so extracted test failures are
    routed to agent repair instead of a misleading CI rerun.

## Evidence

Files changed:

- `src/awf/runtime/ci_failure_evidence.py`
- `src/awf/runtime/pr_monitor.py`
- `src/awf/common/github_client.py`
- `src/awf/runtime/monitor_prompts.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/common/test_github_client.py`
- `tests/unit/runtime/test_monitor_prompts.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `tests/integration/runtime/test_pr_monitor_runner.py`
- `plans/github_ci_failure_evidence_repair_PLAN.md`
- `plans/github_ci_failure_evidence_repair_VALIDATION.md`

Validation commands run:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/common/test_github_client.py \
  tests/unit/runtime/test_monitor_prompts.py \
  tests/unit/runtime/test_pr_monitor_runner.py \
  -q
```

Result: `231 passed in 102.06s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf tests
```

Result: `462 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 153 source files`.

## Remaining Gaps

None.
