# PR608 CI Fix Plan

## Problem Statement And Scope

PR #608 failed the combined `python-full-coverage` job. The earlier shard-3
failure was already fixed on this branch; the current completed run passed all
coverage shards but failed the final combined gate at 98.97% versus the 99.00%
threshold. The downloaded `full-coverage-report` artifact showed uncovered
line/branch slots in executor flow, validation cleanup, conformance artifact
deposit/cleanup, and one executor helper edge. The fix must add behavior-level
coverage for those paths and must not weaken, skip, or disable any check.

## Requirements Checklist

- Identify the failing GitHub Actions job and root cause from available logs.
- Reproduce the issue with the narrowest local command practical.
- Apply the smallest test change needed to cover the uncovered behavior.
- Add behavior-focused regression coverage for reachable defensive and
  lifecycle edges.
- Run focused verification only; AWF/GitHub own broad validation after the
  agent finishes.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Inspect PR #608 check state and recent run history.
2. Pull failed job logs from the latest completed failed run, or from the
   current run as soon as a job completes with failure.
3. Locate the implicated code/test paths and run a focused repro command.
4. Add focused tests for:
   - conformance artifact directory/create/write cleanup failures;
   - validation cleanup guard planning-artifact deposit ordering;
   - executor lifecycle edges around forge gates, Ollama setup failure,
     push-boundary races, PR head-SHA metadata, and validate-only recovery;
   - agent runtime parsing helper edge cases.
5. Run targeted verification for the changed behavior and record results.
6. Create `plans/PR608_CI_FIX_VALIDATION.md`.
7. Commit the scoped fix locally with a conventional commit message.

## Verification Commands And Pass Criteria

- `gh pr checks 608 --json name,state,bucket,link,startedAt,completedAt,workflow`
  confirms the current CI state used for diagnosis.
- A focused `uv run --python 3.12 --extra dev pytest ... -q` command for the
  failing test or closest local reproduction passes.
- Any focused lint/type command run for changed files passes.
- A focused coverage probe over only the new tests may be used to confirm
  overlap with CI-reported missing source lines/branches, but it is not the
  repository coverage gate.

Full AWF/GitHub validation, coverage gates, and broad CI-equivalent suites are
managed by AWF after agent completion and will not be run locally for this fix.
