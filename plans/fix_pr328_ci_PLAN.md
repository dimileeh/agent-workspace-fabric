# Fix PR 328 CI Plan

## Problem Statement and Scope

PR #328 fails the focused Python CI repro in two areas:

- `ControlWorker._release_terminal_runtime_resources` opens the database-backed
  planning-scope retry resume query before processing terminal runtime release
  candidates, which violates the edge test that uses an exploding session
  factory while candidate release failures are being grouped.
- Three first-party test files exceed the repository's 1,500 line
  maintainability limit.

The scope is limited to fixing those failing behaviors without weakening tests,
quality gates, workflows, or AWF/GitHub-owned validation.

## Requirements Checklist

- [ ] Preserve grouped terminal runtime release failures.
- [ ] Avoid opening the planning-scope retry session when the configured
      terminal runtime release scan limit is empty.
- [ ] Keep every first-party source/test file at or under 1,500 lines.
- [ ] Run only focused repro/validation commands locally.
- [ ] Commit the local fix on the current AWF branch without pushing.

## Implementation Steps

1. Inspect the terminal runtime release control flow and update it so an empty
   release scan limit short-circuits before any candidate or retry-resume DB
   queries.
2. Re-run the targeted runtime-release edge test.
3. Split or trim oversized test modules using existing test-part patterns,
   preserving the same test behavior.
4. Re-run the focused CI repro command and any narrow import/type checks needed
   for moved tests.
5. Record validation evidence in `plans/fix_pr328_ci_VALIDATION.md`.
6. Commit the scoped fix locally with a conventional commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- Any new split test file imports cleanly through the targeted pytest command.
- Full AWF/GitHub validation remains owned by AWF after agent completion.
